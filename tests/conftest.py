"""Shared pytest fixtures for the AUPU Q360 integration tests."""

from __future__ import annotations

import socket
import subprocess
import sys
from collections.abc import Generator
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


@pytest.fixture
def project_root() -> Path:
    """Return the repository root without relying on the caller's directory."""
    return _PROJECT_ROOT


@pytest.fixture
def git_repository(tmp_path: Path) -> Path:
    """Create a staged-only Git repository for tracked-file scanner tests."""
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(
        ["git", "init", "--quiet", str(repository)],
        check=True,
        capture_output=True,
    )
    (repository / ".gitignore").write_text(
        ".private/\n"
        "local-evidence/\n"
        "*.har\n"
        "*.saz\n"
        "*.cap\n"
        "*.pcap\n"
        "*.pcapng\n"
        "*.pem\n"
        "*.cer\n"
        "*.crt\n"
        "*.p12\n"
        "*.pfx\n"
        "*.key\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "-C", str(repository), "add", ".gitignore"],
        check=True,
        capture_output=True,
    )
    return repository


@pytest.fixture(autouse=True)
def block_unmocked_network(monkeypatch: pytest.MonkeyPatch) -> Generator[None]:
    """Fail fast if a test attempts DNS or TCP access without an explicit mock."""

    def deny_network(*args: object, **kwargs: object) -> None:
        del args, kwargs
        msg = "Network access is disabled in tests; explicitly mock the request."
        raise AssertionError(msg)

    original_socket = socket.socket

    class NoNetworkSocket(original_socket):
        """Block outbound TCP while leaving local Unix sockets usable."""

        def connect(self, address: object) -> None:
            if self.family in (socket.AF_INET, socket.AF_INET6):
                deny_network(address)
            super().connect(address)

        def connect_ex(self, address: object) -> int:
            if self.family in (socket.AF_INET, socket.AF_INET6):
                deny_network(address)
            return super().connect_ex(address)

    monkeypatch.setattr(socket, "getaddrinfo", deny_network)
    monkeypatch.setattr(socket, "create_connection", deny_network)
    monkeypatch.setattr(socket, "socket", NoNetworkSocket)
    yield
