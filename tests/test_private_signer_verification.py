"""Tests for portable discovery of private signer verification inputs."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.verify_private_signer import (
    discover_capture_root,
    discover_private_project_root,
)

_CAPTURE_RELATIVE_PATHS = (
    Path("A-idle/A-idle.har"),
    Path("B-light-on/B-light-on.har"),
    Path("C-light-off/C-light-off.har"),
)


def _create_capture_candidate(temporary_root: Path, name: str) -> Path:
    candidate = temporary_root / name
    for relative_path in _CAPTURE_RELATIVE_PATHS:
        capture = candidate / relative_path
        capture.parent.mkdir(parents=True, exist_ok=True)
        capture.write_text("{}", encoding="utf-8")
    return candidate


def _run_git(repository: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-C", str(repository), *arguments],
        capture_output=True,
        check=True,
        text=True,
    )


@pytest.mark.parametrize("candidate_count", [0, 1, 2])
def test_capture_discovery_accepts_exactly_one_complete_temporary_candidate(
    tmp_path: Path,
    candidate_count: int,
) -> None:
    """Zero or ambiguous automatic capture candidates must resolve to no input."""
    candidates = [
        _create_capture_candidate(tmp_path, f"wechat-q360t5-capture-synthetic-{index}")
        for index in range(candidate_count)
    ]

    discovered = discover_capture_root(None, {}, tmp_path)

    assert discovered == (candidates[0] if candidate_count == 1 else None)


def test_capture_discovery_prioritizes_cli_then_dedicated_environment(tmp_path: Path) -> None:
    """Explicit sources must not depend on unrelated temporary-directory contents."""
    explicit = tmp_path / "explicit"
    environment = tmp_path / "environment"
    environ = {"AUPU_Q360_CAPTURE_ROOT": str(environment)}

    assert discover_capture_root(explicit, environ, tmp_path) == explicit
    assert discover_capture_root(None, environ, tmp_path) == environment


def test_private_project_discovery_falls_back_to_common_worktree_root(tmp_path: Path) -> None:
    """A linked worktree without materials must reuse the main worktree's local-only files."""
    common_root = tmp_path / "repository"
    common_root.mkdir()
    _run_git(common_root, "init", "--initial-branch=main")
    (common_root / ".gitignore").write_text(".worktrees/\n", encoding="utf-8")
    _run_git(common_root, "add", ".gitignore")
    _run_git(
        common_root,
        "-c",
        "user.name=Synthetic Test",
        "-c",
        "user.email=synthetic@example.invalid",
        "commit",
        "-m",
        "synthetic fixture",
    )
    linked_root = common_root / ".worktrees" / "synthetic-feature"
    _run_git(common_root, "worktree", "add", "-b", "synthetic-feature", str(linked_root))
    secrets = common_root / ".private" / "signer_secrets.json"
    safe_fixture = common_root / "local-evidence" / "signer" / "signer-verification.safe.json"
    secrets.parent.mkdir(parents=True)
    safe_fixture.parent.mkdir(parents=True)
    secrets.write_text("{}", encoding="utf-8")
    safe_fixture.write_text("{}", encoding="utf-8")

    discovered = discover_private_project_root(None, linked_root)

    assert discovered == common_root
