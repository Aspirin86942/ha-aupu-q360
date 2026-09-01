"""Verify local signer secrets against private HAR captures without exposing them."""

from __future__ import annotations

import argparse
import hmac
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT_ROOT))

from custom_components.aupu_q360.signer import AppAuthorizationSigner, SignerSecrets

_CAPTURE_FILES = (
    Path("A-idle/A-idle.har"),
    Path("B-light-on/B-light-on.har"),
    Path("C-light-off/C-light-off.har"),
)
_SAFE_FILE = Path("local-evidence/signer/signer-verification.safe.json")
_SECRETS_FILE = Path(".private/signer_secrets.json")
_CAPTURE_ROOT_ENV = "AUPU_Q360_CAPTURE_ROOT"


class VerificationError(Exception):
    """A verification input is malformed without including its private contents."""


def _parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--private-project-root",
        type=Path,
        help="Project root that contains .private/ and local-evidence/.",
    )
    parser.add_argument(
        "--capture-root",
        type=Path,
        help="Root directory containing the three unredacted HAR capture folders.",
    )
    return parser.parse_args()


def _has_private_materials(project_root: Path) -> bool:
    return (project_root / _SECRETS_FILE).is_file() and (project_root / _SAFE_FILE).is_file()


def _git_common_worktree_root(repository_root: Path) -> Path | None:
    if not (repository_root / ".git").is_file():
        return None
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(repository_root),
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            ],
            capture_output=True,
            check=False,
            text=True,
        )
    except OSError:
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    common_directory = Path(result.stdout.strip())
    if not common_directory.is_absolute():
        common_directory = repository_root / common_directory
    return common_directory.resolve().parent


def discover_private_project_root(
    explicit_root: Path | None,
    repository_root: Path,
    common_worktree_root: Path | None = None,
) -> Path:
    """Choose local-only materials, including a linked-worktree fallback."""
    if explicit_root is not None:
        return explicit_root
    if _has_private_materials(repository_root):
        return repository_root
    fallback = common_worktree_root or _git_common_worktree_root(repository_root)
    if fallback is not None and _has_private_materials(fallback):
        return fallback
    return repository_root


def discover_capture_root(
    explicit_root: Path | None,
    environ: Mapping[str, str],
    temporary_root: Path,
) -> Path | None:
    """Select an explicit capture root or one unambiguous temporary candidate."""
    if explicit_root is not None:
        return explicit_root
    environment_root = environ.get(_CAPTURE_ROOT_ENV)
    if environment_root:
        return Path(environment_root)
    candidates = [
        candidate
        for candidate in temporary_root.glob("wechat-q360t5-capture-*")
        if candidate.is_dir()
        and all((candidate / relative_path).is_file() for relative_path in _CAPTURE_FILES)
    ]
    return candidates[0] if len(candidates) == 1 else None


def _load_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VerificationError(f"{label} is unreadable") from exc
    if not isinstance(value, dict):
        raise VerificationError(f"{label} must contain an object")
    return value


def _require_safe_fixture(value: Mapping[str, Any]) -> list[dict[str, Any]]:
    captured_request_count = value.get("captured_request_count")
    exact_match_count = value.get("exact_match_count")
    requests = value.get("requests")
    if (
        type(captured_request_count) is not int
        or type(exact_match_count) is not int
        or not isinstance(requests, list)
    ):
        raise VerificationError("safe verification fixture has an unexpected structure")
    if captured_request_count != 7 or exact_match_count != 7 or len(requests) != 7:
        raise VerificationError("safe verification fixture must declare seven exact matches")

    validated: list[dict[str, Any]] = []
    for request in requests:
        if not isinstance(request, dict):
            raise VerificationError("safe verification fixture has an unexpected request")
        required_request_types: dict[str, type[object]] = {
            "method": str,
            "path": str,
            "timestamp_delta_from_har_seconds": int,
        }
        if (
            any(
                not isinstance(request.get(key), value_type)
                for key, value_type in required_request_types.items()
            )
            or request.get("exact_match") is not True
        ):
            raise VerificationError("safe verification fixture has an unexpected request")
        validated.append(request)
    return validated


def _header_value(headers: object) -> str | None:
    if not isinstance(headers, list):
        raise VerificationError("HAR request headers have an unexpected structure")
    for header in headers:
        if not isinstance(header, dict):
            raise VerificationError("HAR request headers have an unexpected structure")
        name = header.get("name")
        value = header.get("value")
        if isinstance(name, str) and name.lower() == "app-authorization":
            if not isinstance(value, str):
                raise VerificationError("HAR App-Authorization header is invalid")
            return value
    return None


def _har_seconds(value: object) -> int:
    if not isinstance(value, str):
        raise VerificationError("HAR timestamp is invalid")
    try:
        return int(datetime.fromisoformat(value).timestamp())
    except ValueError as exc:
        raise VerificationError("HAR timestamp is invalid") from exc


def _load_captured_requests(capture_root: Path) -> list[dict[str, Any]]:
    captured: list[dict[str, Any]] = []
    for relative_path in _CAPTURE_FILES:
        har = _load_json_object(capture_root / relative_path, label="HAR capture")
        log = har.get("log")
        entries = log.get("entries") if isinstance(log, dict) else None
        if not isinstance(entries, list):
            raise VerificationError("HAR capture has an unexpected structure")
        for entry in entries:
            if not isinstance(entry, dict):
                raise VerificationError("HAR capture has an unexpected structure")
            request = entry.get("request")
            if not isinstance(request, dict):
                raise VerificationError("HAR capture has an unexpected structure")
            header = _header_value(request.get("headers"))
            if header is None:
                continue
            method = request.get("method")
            url = request.get("url")
            if not isinstance(method, str) or not isinstance(url, str):
                raise VerificationError("HAR request has an unexpected structure")
            captured.append(
                {
                    "method": method,
                    "path": urlsplit(url).path,
                    "header": header,
                    "har_seconds": _har_seconds(entry.get("startedDateTime")),
                }
            )
    return captured


def _find_safe_request(
    captured: Mapping[str, Any],
    safe_requests: Sequence[Mapping[str, Any]],
    used_indexes: set[int],
    signer: AppAuthorizationSigner,
) -> tuple[int | None, bool]:
    header = captured["header"]
    assert isinstance(header, str)
    try:
        timestamp = signer.timestamp_from_header(header)
    except ValueError:
        return None, False

    for index, expected in enumerate(safe_requests):
        if index in used_indexes:
            continue
        if expected["method"] != captured["method"] or expected["path"] != captured["path"]:
            continue
        har_seconds = captured["har_seconds"]
        assert isinstance(har_seconds, int)
        if timestamp - har_seconds == expected["timestamp_delta_from_har_seconds"]:
            return index, hmac.compare_digest(header, signer.sign(timestamp))
    return None, False


def run(private_project_root: Path, capture_root: Path | None) -> int:
    """Run the private comparison, or safely skip when private inputs are missing."""
    secrets_path = private_project_root / _SECRETS_FILE
    safe_path = private_project_root / _SAFE_FILE
    if capture_root is None:
        print("SKIP: private signer verification materials are unavailable.")
        return 0
    capture_paths = [capture_root / path for path in _CAPTURE_FILES]
    if (
        not secrets_path.is_file()
        or not safe_path.is_file()
        or not all(path.is_file() for path in capture_paths)
    ):
        print("SKIP: private signer verification materials are unavailable.")
        return 0

    try:
        safe_fixture = _load_json_object(safe_path, label="safe verification fixture")
        safe_requests = _require_safe_fixture(safe_fixture)
        signer = AppAuthorizationSigner(SignerSecrets.load(secrets_path))
        captured_requests = _load_captured_requests(capture_root)
        used_indexes: set[int] = set()
        exact_match_count = 0
        paths: list[str] = []
        for captured in captured_requests:
            paths.append(str(captured["path"]))
            matched_index, exact_match = _find_safe_request(
                captured, safe_requests, used_indexes, signer
            )
            if matched_index is not None:
                used_indexes.add(matched_index)
            if exact_match:
                exact_match_count += 1

        request_count = len(captured_requests)
        expected_request_count = safe_fixture["captured_request_count"]
        expected_exact_match_count = safe_fixture["exact_match_count"]
        assert isinstance(expected_request_count, int)
        assert isinstance(expected_exact_match_count, int)
        expected_counts_match = (
            request_count == expected_request_count
            and exact_match_count == expected_exact_match_count
            and len(used_indexes) == len(safe_requests)
        )
        all_headers_match = request_count == exact_match_count
        print(
            json.dumps(
                {
                    "request_count": request_count,
                    "exact_match_count": exact_match_count,
                    "paths": paths,
                    "expected_counts_match": expected_counts_match,
                    "all_headers_match": all_headers_match,
                },
                ensure_ascii=False,
            )
        )
        return 0 if expected_counts_match and all_headers_match else 1
    except (OSError, TypeError, ValueError, VerificationError):
        print("FAIL: private signer verification could not run safely.")
        return 1


def main() -> int:
    """Parse CLI arguments and return the verification status."""
    args = _parse_arguments()
    private_project_root = discover_private_project_root(
        args.private_project_root,
        _PROJECT_ROOT,
    )
    capture_root = discover_capture_root(
        args.capture_root,
        os.environ,
        Path(tempfile.gettempdir()),
    )
    return run(private_project_root, capture_root)


if __name__ == "__main__":
    raise SystemExit(main())
