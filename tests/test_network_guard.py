"""Regression tests for the default offline pytest guard."""

from __future__ import annotations

import socket

import pytest

pytestmark = pytest.mark.enable_socket


def test_blocks_dns_lookup_before_resolving_localhost() -> None:
    """An unmocked DNS lookup must fail before localhost resolution starts."""
    with pytest.raises(AssertionError):
        socket.getaddrinfo("localhost", 443)


def test_blocks_tcp_helper_before_connecting_to_loopback() -> None:
    """The TCP helper must fail before it can connect to IPv4 loopback."""
    with pytest.raises(AssertionError):
        socket.create_connection(("127.0.0.1", 9))


def test_blocks_direct_ipv4_tcp_connect_before_loopback_connection() -> None:
    """Direct IPv4 socket connections must be rejected before loopback I/O."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(AssertionError):
            sock.connect(("127.0.0.1", 9))
    finally:
        sock.close()


def test_blocks_direct_ipv6_tcp_connect_ex_before_loopback_connection() -> None:
    """Direct IPv6 socket connection attempts must be rejected before loopback I/O."""
    sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    try:
        with pytest.raises(AssertionError):
            sock.connect_ex(("::1", 9))
    finally:
        sock.close()
