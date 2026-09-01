"""Scan tracked regular files for sensitive values without echoing file contents."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import tempfile
from collections import Counter
from collections.abc import Iterable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import parse_qsl, urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CAPTURE_FILES = (
    Path("A-idle/A-idle.har"),
    Path("B-light-on/B-light-on.har"),
    Path("C-light-off/C-light-off.har"),
)

_PRIVATE_ROOT_ENV = "AUPU_Q360_PRIVATE_PROJECT_ROOT"
_CAPTURE_ROOT_ENV = "AUPU_Q360_CAPTURE_ROOT"
_SECRETS_FILE = Path(".private/signer_secrets.json")
_IGNORE_SAMPLES = (
    ".private/probe",
    "local-evidence/probe",
    "probe.har",
    "probe.saz",
    "probe.cap",
    "probe.pcap",
    "probe.pcapng",
    "probe.pem",
    "probe.cer",
    "probe.crt",
    "probe.p12",
    "probe.pfx",
    "probe.key",
)

_PATTERNS = {
    "jwt": re.compile(
        rb"(?<![A-Za-z0-9_-])(?:eyJ|e30)[A-Za-z0-9_-]{7,}\."
        rb"[A-Za-z0-9_-]{10,}\."
        rb"[A-Za-z0-9_-]{10,}(?![A-Za-z0-9_-])"
    ),
    "bearer": re.compile(
        rb"\bBearer[ \t]+[A-Za-z0-9._~+/=-]{20,}", re.IGNORECASE
    ),
    "phone": re.compile(
        rb"(?<![A-Za-z0-9])1[3-9][0-9]{9}(?![A-Za-z0-9])"
    ),
    "private_key": re.compile(
        rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"
    ),
    "app_authorization": re.compile(
        rb"\bApp-Authorization\b[ \t\"']*[:=][ \t\"']*"
        rb"[^\r\n]{0,192}[0-9]{10}[^\r\n]{0,192}[A-Za-z0-9+/=]{32,}",
        re.IGNORECASE,
    ),
    "assignment": re.compile(
        rb"\b(?:token|api[_-]?token|access[_-]?token|refresh[_-]?token|"
        rb"auth[_-]?token|cookie|client[_-]?secret|api[_-]?secret|password)"
        rb"[ \t\"']*[:=][ \t\"']+"
        rb"[A-Za-z0-9._~+/=-]{20,}",
        re.IGNORECASE,
    ),
}

_ALLOWED_SYNTHETIC_VALUES = frozenset(
    {
        b"syntheticFixtureHeader."
        + b"syntheticFixturePayload."
        + b"syntheticFixtureSignature",
        b"Bearer " + b"synthetic-fixture-token-" + b"000000000",
        b"138" + b"0000" + b"0000",
    }
)
_SENSITIVE_NAME_PARTS = (
    "authorization",
    "bearer",
    "token",
    "cookie",
    "session",
    "signature",
    "phone",
    "did",
    "deviceid",
    "clientid",
    "useruuid",
    "credential",
    "secret",
    "appkey",
)


class ScanFailure(Exception):
    """A fixed scanner failure that never incorporates private input."""


def _parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--private-project-root", type=Path)
    parser.add_argument("--capture-root", type=Path)
    return parser.parse_args()


def _git(repository_root: Path, arguments: list[str], *, input_bytes: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            ["git", "-C", str(repository_root), *arguments],
            input=input_bytes,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise ScanFailure from exc


def _tracked_regular_files(repository_root: Path) -> list[tuple[str, Path]]:
    result = _git(repository_root, ["ls-files", "-z"])
    if result.returncode != 0:
        raise ScanFailure
    try:
        relative_paths = [
            item.decode("utf-8") for item in result.stdout.split(b"\0") if item
        ]
    except UnicodeDecodeError as exc:
        raise ScanFailure from exc

    tracked: list[tuple[str, Path]] = []
    root = repository_root.resolve()
    for relative_path in relative_paths:
        pure_path = PurePosixPath(relative_path)
        if pure_path.is_absolute() or ".." in pure_path.parts:
            raise ScanFailure
        file_path = root.joinpath(*pure_path.parts)
        try:
            mode = file_path.stat(follow_symlinks=False).st_mode
        except OSError as exc:
            raise ScanFailure from exc
        if stat.S_ISREG(mode):
            tracked.append((relative_path, file_path))
    return tracked


def _missing_ignore_rules(repository_root: Path) -> list[str]:
    encoded = b"\0".join(sample.encode("utf-8") for sample in _IGNORE_SAMPLES) + b"\0"
    result = _git(
        repository_root,
        ["check-ignore", "--no-index", "-z", "--stdin"],
        input_bytes=encoded,
    )
    if result.returncode not in (0, 1):
        raise ScanFailure
    try:
        ignored = {
            item.decode("utf-8") for item in result.stdout.split(b"\0") if item
        }
    except UnicodeDecodeError as exc:
        raise ScanFailure from exc
    return [sample for sample in _IGNORE_SAMPLES if sample not in ignored]


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ScanFailure from exc


def _all_non_empty_strings(value: object) -> Iterable[str]:
    if isinstance(value, str):
        if value:
            yield value
        return
    if isinstance(value, Mapping):
        for nested in value.values():
            yield from _all_non_empty_strings(nested)
        return
    if isinstance(value, list):
        for nested in value:
            yield from _all_non_empty_strings(nested)


def _normalized_name(value: str) -> str:
    return "".join(character for character in value.lower() if character.isalnum())


def _is_sensitive_name(value: str) -> bool:
    normalized = _normalized_name(value)
    return any(part in normalized for part in _SENSITIVE_NAME_PARTS)


def _har_sensitive_strings(value: object) -> Iterable[str]:
    if isinstance(value, Mapping):
        name = value.get("name")
        if isinstance(name, str) and _is_sensitive_name(name):
            yield from _all_non_empty_strings(value.get("value"))
        for key, nested in value.items():
            if not isinstance(key, str):
                continue
            if _is_sensitive_name(key):
                yield from _all_non_empty_strings(nested)
            else:
                yield from _har_sensitive_strings(nested)
            if key.lower() == "url" and isinstance(nested, str):
                try:
                    for query_name, query_value in parse_qsl(
                        urlsplit(nested).query, keep_blank_values=False
                    ):
                        if query_value and _is_sensitive_name(query_name):
                            yield query_value
                except ValueError:
                    continue
            if key.lower() in {"text", "body"} and isinstance(nested, str):
                stripped = nested.lstrip()
                if stripped.startswith(("{", "[")):
                    try:
                        embedded = json.loads(nested)
                    except json.JSONDecodeError:
                        continue
                    yield from _har_sensitive_strings(embedded)
        return
    if isinstance(value, list):
        for nested in value:
            yield from _har_sensitive_strings(nested)


def _private_candidates(
    private_project_root: Path | None,
    capture_root: Path | None,
) -> tuple[list[bytes], bool]:
    candidates: set[bytes] = set()
    available = False
    if private_project_root is not None:
        secrets_path = private_project_root / _SECRETS_FILE
        if secrets_path.is_file():
            available = True
            for value in _all_non_empty_strings(_read_json(secrets_path)):
                candidates.add(value.encode("utf-8"))

    if capture_root is not None:
        capture_paths = [capture_root / relative_path for relative_path in CAPTURE_FILES]
        existing = [path.is_file() for path in capture_paths]
        if any(existing):
            available = True
            if not all(existing):
                raise ScanFailure
            for capture_path in capture_paths:
                for value in _har_sensitive_strings(_read_json(capture_path)):
                    candidates.add(value.encode("utf-8"))
    return list(candidates), available


def _scan_files(
    tracked_files: list[tuple[str, Path]],
    private_candidates: list[bytes],
) -> Counter[tuple[str, str]]:
    hits: Counter[tuple[str, str]] = Counter()
    try:
        for relative_path, file_path in tracked_files:
            try:
                content = file_path.read_bytes()
            except OSError as exc:
                raise ScanFailure from exc
            for hit_type, pattern in _PATTERNS.items():
                for match in pattern.finditer(content):
                    if not _allowed_match(hit_type, content, match):
                        hits[(hit_type, relative_path)] += 1
            for candidate in private_candidates:
                if _private_candidate_occurs(candidate, content):
                    hits[("exact_private_value", relative_path)] += 1
    finally:
        private_candidates.clear()
    return hits


def _allowed_match(hit_type: str, content: bytes, match: re.Match[bytes]) -> bool:
    matched = match.group(0)
    if matched in _ALLOWED_SYNTHETIC_VALUES:
        return True
    if hit_type != "phone":
        return False
    line_start = content.rfind(b"\n", 0, match.start()) + 1
    prefix = content[line_start : match.start()]
    return re.search(rb"\bsize[ \t]*=[ \t]*$", prefix, re.IGNORECASE) is not None


def _private_candidate_occurs(candidate: bytes, content: bytes) -> bool:
    if not candidate:
        return False
    if len(candidate) < 12:
        return content.strip() == candidate
    has_alpha = any(65 <= value <= 90 or 97 <= value <= 122 for value in candidate)
    has_digit = any(48 <= value <= 57 for value in candidate)
    if has_alpha and has_digit:
        return candidate in content
    if has_digit:
        return (
            content.strip() == candidate
            or b'"' + candidate + b'"' in content
            or b"'" + candidate + b"'" in content
        )
    return content.strip() == candidate


def _safe_relative_path(value: str) -> str:
    return "".join(
        character if character.isalnum() or character in "._/-" else "?"
        for character in value
    )


def _print_hits(hits: Counter[tuple[str, str]]) -> int:
    total = sum(hits.values())
    for (hit_type, relative_path), count in sorted(hits.items()):
        print(
            f"hit_type={hit_type} file={_safe_relative_path(relative_path)} count={count}"
        )
    print(f"sensitive_hit_count={total}")
    return total


def run(
    repository_root: Path,
    private_project_root: Path | None,
    capture_root: Path | None,
) -> int:
    """Scan one Git index and report only fixed categories, relative paths, and counts."""
    try:
        tracked_files = _tracked_regular_files(repository_root)
    except ScanFailure:
        print("checked_file_count=0")
        print("private_sources=unavailable")
        _print_hits(Counter({("git_failure", "."): 1}))
        return 1

    print(f"checked_file_count={len(tracked_files)}")
    try:
        missing_rules = _missing_ignore_rules(repository_root)
    except ScanFailure:
        print("private_sources=unavailable")
        _print_hits(Counter({("git_failure", "."): 1}))
        return 1

    hits: Counter[tuple[str, str]] = Counter(
        {("missing_ignore_rule", sample): 1 for sample in missing_rules}
    )
    try:
        private_candidates, private_available = _private_candidates(
            private_project_root, capture_root
        )
    except ScanFailure:
        print("private_sources=available")
        hits[("private_source_error", ".")] += 1
        _print_hits(hits)
        return 1

    print(f"private_sources={'available' if private_available else 'unavailable'}")
    try:
        hits.update(_scan_files(tracked_files, private_candidates))
    except ScanFailure:
        private_candidates.clear()
        hits[("tracked_file_error", ".")] += 1
    return 1 if _print_hits(hits) else 0


def _git_common_worktree_root(repository_root: Path) -> Path | None:
    result = _git(
        repository_root,
        ["rev-parse", "--path-format=absolute", "--git-common-dir"],
    )
    if result.returncode != 0:
        return None
    try:
        common_directory = Path(result.stdout.decode("utf-8").strip())
    except UnicodeDecodeError:
        return None
    if not common_directory.is_absolute():
        common_directory = repository_root / common_directory
    return common_directory.resolve().parent


def _discover_private_project_root(
    explicit_root: Path | None,
    repository_root: Path,
) -> Path | None:
    if explicit_root is not None:
        return explicit_root
    environment_root = os.environ.get(_PRIVATE_ROOT_ENV)
    if environment_root:
        return Path(environment_root)
    if (repository_root / _SECRETS_FILE).is_file():
        return repository_root
    common_root = _git_common_worktree_root(repository_root)
    if common_root is not None and (common_root / _SECRETS_FILE).is_file():
        return common_root
    return None


def _discover_capture_root(explicit_root: Path | None) -> Path | None:
    if explicit_root is not None:
        return explicit_root
    environment_root = os.environ.get(_CAPTURE_ROOT_ENV)
    if environment_root:
        return Path(environment_root)
    candidates = [
        candidate
        for candidate in Path(tempfile.gettempdir()).glob("wechat-q360t5-capture-*")
        if candidate.is_dir()
        and all((candidate / relative_path).is_file() for relative_path in CAPTURE_FILES)
    ]
    return candidates[0] if len(candidates) == 1 else None


def main() -> int:
    """Parse local-only source locations and run the safe scanner."""
    args = _parse_arguments()
    repository_root = args.repository_root
    private_project_root = _discover_private_project_root(
        args.private_project_root, repository_root
    )
    capture_root = _discover_capture_root(args.capture_root)
    return run(repository_root, private_project_root, capture_root)


if __name__ == "__main__":
    raise SystemExit(main())
