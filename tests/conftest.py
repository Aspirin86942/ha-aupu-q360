"""Shared pytest fixtures for the AUPU Q360 integration tests."""

from __future__ import annotations

import socket
from collections.abc import Generator
from pathlib import Path

import pytest


@pytest.fixture
def project_root() -> Path:
    """Return the repository root without relying on the caller's directory."""
    return Path(__file__).resolve().parents[1]


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
