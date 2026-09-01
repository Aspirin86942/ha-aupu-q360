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
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from urllib.parse import parse_qsl, quote, quote_plus, urlsplit

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
        rb"-----BEGIN (?:ENCRYPTED |RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"
    ),
    "app_authorization": re.compile(
        rb"\bApp-Authorization\b[ \t\"']*[:=][ \t\"']*"
        rb"[^\r\n]{0,192}[0-9]{10}[^\r\n]{0,192}[A-Za-z0-9+/=]{32,}",
        re.IGNORECASE,
    ),
    "assignment": re.compile(
        rb"\b(?:"
        rb"(?:[A-Za-z][A-Za-z0-9]*[_-])?(?:secret|signature)|"
        rb"jwt(?:[_-]?token)?|id[_-]?token|private[_-]?key|"
        rb"token|api[_-]?token|access[_-]?token|refresh[_-]?token|"
        rb"auth[_-]?token|cookie|password)\b(?:"
        rb"[ \t\"']*:[ \t]*(?:[\"']"
        rb"(?P<assignment_colon>[A-Za-z0-9._~+/=-]{20,})[\"']|"
        rb"(?P<assignment_yaml_unquoted>[A-Za-z0-9._~+/=-]{20,})"
        rb"(?=[ \t]*(?:#[^\r\n]*)?\r?$))|"
        rb"[ \t]*=[ \t]*(?:[\"']"
        rb"(?P<assignment_quoted>[A-Za-z0-9._~+/=-]{20,})[\"']|"
        rb"(?P<assignment_unquoted>[A-Za-z0-9._~+/=-]{20,})"
        rb"(?=[ \t]*(?:[#;][^\r\n]*)?\r?$)))",
        re.IGNORECASE | re.MULTILINE,
    ),
}

_ALLOWED_SYNTHETIC_VALUES = frozenset(
    {
        b"syntheticFixtureHeader."
        + b"syntheticFixturePayload."
        + b"syntheticFixtureSignature",
        b"Bearer " + b"synthetic-fixture-token-" + b"000000000",
        b"138" + b"0000" + b"0000",
        b"synthetic-sensitive-" + b"exception-sentinel",
        b"unloaded-" + b"private-value",
        b"synthetic-signature-" + b"secret",
        b"synthetic-signature-" + b"1",
    }
)
_SENSITIVE_NAMES = frozenset(
    {
    "authorization",
    "appauthorization",
    "bearer",
    "token",
    "accesstoken",
    "refreshtoken",
    "authtoken",
    "idtoken",
    "jwt",
    "cookie",
    "session",
    "sessionid",
    "jsessionid",
    "sign",
    "sig",
    "signature",
    "signaturevalue",
    "appsignature",
    "xappsign",
    "phone",
    "phonenumber",
    "mobile",
    "mobilenumber",
    "mobilephone",
    "cellphone",
    "msisdn",
    "did",
    "deviceid",
    "deviceidentifier",
    "deviceuuid",
    "clientid",
    "useruuid",
    "credential",
    "secret",
    "appkey",
    }
)
_LOW_SIGNER_FIELDS = frozenset(
    {
        "packagename",
        "sdkversion",
        "messageprefix",
        "sdklabel",
        "typetimestamplabel",
        "headerprefix",
        "headersep1",
        "headersep2",
        "signaturelabel",
    }
)
_IDENTIFIER_NAMES = frozenset(
    {
        "phone",
        "phonenumber",
        "mobile",
        "mobilenumber",
        "mobilephone",
        "cellphone",
        "msisdn",
        "did",
        "deviceid",
        "deviceidentifier",
        "deviceuuid",
        "clientid",
        "useruuid",
        "cookie",
        "session",
        "sessionid",
        "jsessionid",
    }
)

CandidateSource = Literal[
    "signer",
    "har_header",
    "har_query",
    "har_parameter",
    "har_cookie",
    "har_json",
]
CandidateSensitivity = Literal["high", "low"]


@dataclass(frozen=True, slots=True, repr=False)
class _PrivateCandidate:
    value: str
    source: CandidateSource
    sensitivity: CandidateSensitivity
    field_name: str


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
    return _normalized_name(value) in _SENSITIVE_NAMES


def _har_sensitive_strings(value: object) -> Iterable[str]:
    for _, _, sensitive_value in _har_sensitive_items(value):
        yield sensitive_value


def _har_sensitive_items(value: object) -> Iterable[tuple[CandidateSource, str, str]]:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if not isinstance(key, str):
                continue
            normalized_key = _normalized_name(key)
            if normalized_key == "headers":
                yield from _named_har_values(nested, "har_header")
            elif normalized_key == "querystring":
                yield from _named_har_values(nested, "har_query")
            elif normalized_key in {"params", "parameters"}:
                yield from _named_har_values(nested, "har_parameter")
            elif normalized_key == "cookies":
                yield from _cookie_har_values(nested)
            elif normalized_key == "url" and isinstance(nested, str):
                try:
                    for query_name, query_value in parse_qsl(
                        urlsplit(nested).query, keep_blank_values=False
                    ):
                        if query_value and _is_sensitive_name(query_name):
                            yield "har_query", query_name, query_value
                except ValueError:
                    continue
            elif normalized_key in {"text", "body"} and isinstance(nested, str):
                stripped = nested.lstrip()
                if stripped.startswith(("{", "[")):
                    try:
                        embedded = json.loads(nested)
                    except json.JSONDecodeError:
                        continue
                    yield from _json_sensitive_items(embedded)
            else:
                yield from _har_sensitive_items(nested)
        return
    if isinstance(value, list):
        for nested in value:
            yield from _har_sensitive_items(nested)


def _named_har_values(
    value: object,
    source: Literal["har_header", "har_query", "har_parameter"],
) -> Iterable[tuple[CandidateSource, str, str]]:
    if not isinstance(value, list):
        return
    for item in value:
        if not isinstance(item, Mapping):
            continue
        name = item.get("name")
        candidate = item.get("value")
        if (
            isinstance(name, str)
            and isinstance(candidate, str)
            and candidate
            and _is_sensitive_name(name)
        ):
            yield source, name, candidate


def _cookie_har_values(value: object) -> Iterable[tuple[CandidateSource, str, str]]:
    if not isinstance(value, list):
        return
    for item in value:
        if not isinstance(item, Mapping):
            continue
        candidate = item.get("value")
        name = item.get("name")
        if isinstance(candidate, str) and candidate:
            yield "har_cookie", name if isinstance(name, str) else "cookie", candidate


def _json_sensitive_items(value: object) -> Iterable[tuple[CandidateSource, str, str]]:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if not isinstance(key, str):
                continue
            if _is_sensitive_name(key) and isinstance(nested, str) and nested:
                yield "har_json", key, nested
            elif isinstance(nested, Mapping | list):
                yield from _json_sensitive_items(nested)
        return
    if isinstance(value, list):
        for nested in value:
            yield from _json_sensitive_items(nested)


def _signer_candidates(value: object, field_name: str = "value") -> Iterable[_PrivateCandidate]:
    if isinstance(value, str):
        if value:
            sensitivity: CandidateSensitivity = (
                "low" if _normalized_name(field_name) in _LOW_SIGNER_FIELDS else "high"
            )
            yield _PrivateCandidate(value, "signer", sensitivity, field_name)
        return
    if isinstance(value, Mapping):
        for key, nested in value.items():
            yield from _signer_candidates(nested, str(key))
        return
    if isinstance(value, list):
        for nested in value:
            yield from _signer_candidates(nested, field_name)


def _private_candidates(
    private_project_root: Path | None,
    capture_root: Path | None,
) -> tuple[list[_PrivateCandidate], bool]:
    candidates: set[_PrivateCandidate] = set()
    available = False
    if private_project_root is not None:
        secrets_path = private_project_root / _SECRETS_FILE
        if secrets_path.is_file():
            available = True
            candidates.update(_signer_candidates(_read_json(secrets_path)))

    if capture_root is not None:
        capture_paths = [capture_root / relative_path for relative_path in CAPTURE_FILES]
        existing = [path.is_file() for path in capture_paths]
        if any(existing):
            available = True
            if not all(existing):
                raise ScanFailure
            for capture_path in capture_paths:
                for source, field_name, value in _har_sensitive_items(
                    _read_json(capture_path)
                ):
                    sensitivity: CandidateSensitivity = (
                        "low"
                        if source == "har_cookie"
                        or _normalized_name(field_name) in _IDENTIFIER_NAMES
                        else "high"
                    )
                    candidates.add(
                        _PrivateCandidate(value, source, sensitivity, field_name)
                    )
    return list(candidates), available


def _scan_files(
    tracked_files: list[tuple[str, Path]],
    private_candidates: list[_PrivateCandidate],
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
    if (
        hit_type == "assignment"
        and next(
            (
                value
                for value in (
                    match.group("assignment_colon"),
                    match.group("assignment_yaml_unquoted"),
                    match.group("assignment_quoted"),
                    match.group("assignment_unquoted"),
                )
                if value is not None
            ),
            None,
        )
        in _ALLOWED_SYNTHETIC_VALUES
    ):
        return True
    if hit_type != "phone":
        return False
    line_start = content.rfind(b"\n", 0, match.start()) + 1
    prefix = content[line_start : match.start()]
    return re.search(rb"\bsize[ \t]*=[ \t]*$", prefix, re.IGNORECASE) is not None


def _private_candidate_occurs(candidate: _PrivateCandidate, content: bytes) -> bool:
    raw = candidate.value.encode("utf-8")
    if candidate.sensitivity == "high":
        return any(variant in content for variant in _candidate_variants(candidate.value))
    if content.strip() == raw:
        return True
    try:
        decoded = content.decode("utf-8")
    except UnicodeDecodeError:
        return False
    try:
        document = json.loads(decoded)
    except json.JSONDecodeError:
        document = None
    if _json_document_contains_candidate(document, candidate):
        return True
    stripped = decoded.strip()
    expected_name = _normalized_name(candidate.field_name)
    try:
        query = parse_qsl(urlsplit(stripped).query, keep_blank_values=True)
    except ValueError:
        query = []
    if any(
        _normalized_name(name) == expected_name and value == candidate.value
        for name, value in query
    ):
        return True

    if (
        "=" not in stripped
        or any(character.isspace() for character in stripped)
        or any(marker in stripped for marker in ("?", "#"))
    ):
        return False
    segments = stripped.split("&")
    if any(not segment or not segment.partition("=")[0] for segment in segments):
        return False
    try:
        form = parse_qsl(stripped, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return False
    return any(
        _normalized_name(name) == expected_name and value == candidate.value
        for name, value in form
    )


def _json_document_contains_candidate(
    value: object,
    candidate: _PrivateCandidate,
) -> bool:
    if isinstance(value, Mapping):
        expected_name = _normalized_name(candidate.field_name)
        for key, nested in value.items():
            if (
                isinstance(key, str)
                and _normalized_name(key) == expected_name
                and nested == candidate.value
            ):
                return True
            if candidate.source == "har_cookie" and key == "value" and nested == candidate.value:
                name = value.get("name")
                if isinstance(name, str) and _normalized_name(name) == expected_name:
                    return True
            if _json_document_contains_candidate(nested, candidate):
                return True
        return False
    if isinstance(value, list):
        return any(_json_document_contains_candidate(item, candidate) for item in value)
    return False


def _candidate_variants(value: str) -> frozenset[bytes]:
    return frozenset(
        {
            value.encode("utf-8"),
            json.dumps(value, ensure_ascii=True).encode("utf-8"),
            json.dumps(value, ensure_ascii=False).encode("utf-8"),
            quote(value, safe="").encode("ascii"),
            quote_plus(value, safe="").encode("ascii"),
        }
    )


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
