"""Tests for secret-free diagnostics and the repository secret scanner."""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import Coroutine, Generator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest

from custom_components.aupu_q360.auth import BearerCredential
from custom_components.aupu_q360.coordinator import AupuCoordinator
from custom_components.aupu_q360.diagnostics import async_get_config_entry_diagnostics
from custom_components.aupu_q360.shadow import LightShadowUpdate
from scripts import check_no_secrets

_NOW = datetime(2026, 9, 2, 8, 0, tzinfo=UTC)
_TEST_LOOP = asyncio.new_event_loop()
_DIAGNOSTIC_KEYS = {
    "integration_version",
    "authentication_expiry_bucket",
    "wss_enabled",
    "wss_connected",
    "wss_healthy",
    "last_error_code",
    "light_state_source",
    "assumed_state",
}


def _credential(expires_at: datetime | None) -> BearerCredential:
    token = (
        "syntheticHeaderSegment."
        "syntheticPayloadSegment."
        "syntheticSignatureSegment"
    )
    return BearerCredential(_token=token, expires_at=expires_at)


@pytest.fixture(scope="module", autouse=True)
def _close_test_loop() -> Generator[None]:
    """Close the pre-fixture loop used by Windows direct-step calls."""
    yield
    _TEST_LOOP.close()


def _run[T](awaitable: Coroutine[Any, Any, T]) -> T:
    return _TEST_LOOP.run_until_complete(awaitable)


def test_diagnostics_build_only_the_allowed_scalar_whitelist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch diagnostics copying any Config Entry or runtime secret-bearing object."""
    forbidden = {
        "phone": "139" + "2468" + "1357",
        "did": "987654321012345",
        "tag": "private-device-tag",
        "user_uuid": "private-user-uuid",
        "signer": "private-signing-material",
        "wss": "wss://example.invalid/mqtt?credential=private-query-value",
        "entry": "private-entry-identifier",
    }
    coordinator = SimpleNamespace(
        wss_connected=True,
        wss_healthy=False,
        last_error_code="protocol_error",
        light_state_source="reported",
        assumed_state=False,
        forbidden=forbidden,
    )
    runtime = SimpleNamespace(
        credential=_credential(_NOW + timedelta(days=8)),
        use_wss=True,
        coordinator=coordinator,
        device=SimpleNamespace(did=forbidden["did"], tag=forbidden["tag"]),
        signer=forbidden["signer"],
        user_uuid=forbidden["user_uuid"],
        endpoint=forbidden["wss"],
    )
    entry = SimpleNamespace(
        runtime_data=runtime,
        data=forbidden,
        options=forbidden,
        title=forbidden["phone"],
        unique_id=forbidden["entry"],
        entry_id=forbidden["entry"],
    )
    monkeypatch.setattr("custom_components.aupu_q360.diagnostics._utcnow", lambda: _NOW)

    result = _run(async_get_config_entry_diagnostics(None, cast(Any, entry)))

    assert result == {
        "integration_version": "0.1.0",
        "authentication_expiry_bucket": ">=7d",
        "wss_enabled": True,
        "wss_connected": True,
        "wss_healthy": False,
        "last_error_code": "protocol_error",
        "light_state_source": "reported",
        "assumed_state": False,
    }
    serialized = json.dumps(result, sort_keys=True)
    assert set(result) == _DIAGNOSTIC_KEYS
    assert all(value not in serialized for value in forbidden.values())
    assert runtime.credential.authorization_header not in serialized


def test_unloaded_and_incomplete_runtime_keep_the_same_whitelist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch an unloaded entry falling back to serialized Config Entry data."""
    monkeypatch.setattr("custom_components.aupu_q360.diagnostics._utcnow", lambda: _NOW)
    secret = "unloaded-private-value"
    entries = (
        SimpleNamespace(data={"token": secret}, title=secret, unique_id=secret),
        SimpleNamespace(
            data={"token": secret},
            runtime_data=SimpleNamespace(use_wss=True, credential=None, coordinator=None),
        ),
    )

    results = [
        _run(async_get_config_entry_diagnostics(None, cast(Any, entry))) for entry in entries
    ]

    expected = {
        "integration_version": "0.1.0",
        "authentication_expiry_bucket": "unknown",
        "wss_enabled": False,
        "wss_connected": False,
        "wss_healthy": False,
        "last_error_code": "none",
        "light_state_source": "unknown",
        "assumed_state": False,
    }
    assert results == [expected, expected]
    assert all(secret not in json.dumps(result) for result in results)


@pytest.mark.parametrize(
    ("expires_at", "expected"),
    (
        (_NOW - timedelta(microseconds=1), "expired"),
        (_NOW, "expired"),
        (_NOW + timedelta(hours=23, minutes=59), "<24h"),
        (_NOW + timedelta(hours=24), "<7d"),
        (_NOW + timedelta(days=6, hours=23), "<7d"),
        (_NOW + timedelta(days=7), ">=7d"),
        (None, "unknown"),
    ),
)
def test_expiry_diagnostics_use_only_coarse_boundary_buckets(
    monkeypatch: pytest.MonkeyPatch,
    expires_at: datetime | None,
    expected: str,
) -> None:
    """Catch diagnostics exposing exact expiry or mishandling bucket boundaries."""
    monkeypatch.setattr("custom_components.aupu_q360.diagnostics._utcnow", lambda: _NOW)
    entry = SimpleNamespace(
        runtime_data=SimpleNamespace(
            credential=_credential(expires_at),
            use_wss=False,
            coordinator=None,
        )
    )

    result = _run(async_get_config_entry_diagnostics(None, cast(Any, entry)))

    assert result["authentication_expiry_bucket"] == expected
    assert set(result) == _DIAGNOSTIC_KEYS
    assert "2026" not in json.dumps(result)


def test_coordinator_exposes_only_fixed_diagnostic_state() -> None:
    """Catch runtime diagnostics retaining arbitrary exceptions or raw state sources."""
    coordinator = AupuCoordinator(
        hass=cast(Any, SimpleNamespace(data=None)),
        entry_id="synthetic-entry",
        credential=_credential(_NOW + timedelta(days=1)),
        api=cast(Any, object()),
        async_request_reauth=lambda: None,
    )

    coordinator.async_apply_light_state(is_on=True, confirmed=False)
    assert coordinator.light_state_source == "unknown"
    coordinator.async_apply_shadow_update(
        LightShadowUpdate(is_on=False, confirmed=True, source="reported")
    )
    assert coordinator.light_state_source == "reported"
    coordinator.async_handle_wss_auth_failure()

    assert coordinator.last_error_code == "authentication_failed"
    assert isinstance(coordinator.last_error_code, str)


def _track(repository: Any, relative_path: str, content: bytes) -> None:
    path = repository / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    subprocess.run(
        ["git", "-C", str(repository), "add", relative_path],
        check=True,
        capture_output=True,
    )


def test_secret_scanner_detects_each_regex_without_echoing_content(
    git_repository: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Catch missing patterns and any report that repeats a secret or matching line."""
    values = {
        "jwt": b".".join((b"eyJHeaderSegmentA1", b"PayloadSegmentB2", b"SignatureSegmentC3")),
        "bearer": b"Bearer " + b"opaque-production-token-" + b"9876543210",
        "phone": b"139" + b"1234" + b"5678",
        "private_key": b"-----BEGIN " + b"PRIVATE KEY-----",
        "app_authorization": (
            b"App-" + b"Authorization: app=production;ts=1760000000;signature="
            + b"QWxhZGRpbjpPcGVuU2VzYW1lU2lnbmF0dXJlMTIzNDU2Nzg5MA=="
        ),
        "assignment": b"client_" + b"secret = \"" + b"opaqueAssignmentValue9876543210" + b"\"",
    }
    matching_line = b"do-not-print-matching-line " + b" | ".join(values.values())
    _track(git_repository, "leaks.txt", matching_line)

    result = check_no_secrets.run(git_repository, None, None)
    output = capsys.readouterr()

    assert result == 1
    assert output.err == ""
    assert "do-not-print-matching-line" not in output.out
    assert all(value.decode() not in output.out for value in values.values())
    for hit_type in values:
        assert f"hit_type={hit_type}" in output.out
    assert "file=leaks.txt" in output.out


def test_secret_scanner_allows_only_known_synthetic_values_and_skips_untracked_files(
    git_repository: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Catch broad fixture-directory exclusions or scanning outside Git tracked files."""
    known_synthetic = (
        b"syntheticFixtureHeader.syntheticFixturePayload.syntheticFixtureSignature\n"
        b"Bearer synthetic-fixture-token-000000000\n"
        b"13800000000\n"
    )
    _track(git_repository, "tests/fixture_values.txt", known_synthetic)
    _track(
        git_repository,
        "uv.lock",
        b"size = 139" + b"1234" + b"5678\n"
        b"hash = abcdefghijk.lmnopqrstuv.wxyzABCDEFG\n",
    )
    private_value = b".".join((b"OutsideHeader123", b"OutsidePayload456", b"OutsideSignature789"))
    private_file = git_repository / ".private" / "outside.txt"
    private_file.parent.mkdir(parents=True)
    private_file.write_bytes(private_value)

    result = check_no_secrets.run(git_repository, None, None)
    output = capsys.readouterr()

    assert result == 0
    assert output.err == ""
    assert "private_sources=unavailable" in output.out
    assert "sensitive_hit_count=0" in output.out
    assert private_value.decode() not in output.out


def test_secret_scanner_compares_private_json_and_har_values_only_in_memory(
    git_repository: Any,
    tmp_path: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Catch omission of exact private candidates or disclosure in hit reports."""
    private_candidate = b"private" + b"SignerCandidate987654321"
    har_candidate = b"private" + b"HarSessionCandidate123456789"
    private_file = git_repository / ".private" / "signer_secrets.json"
    private_file.parent.mkdir(parents=True)
    private_file.write_text(
        json.dumps({"nested": {"app_key": private_candidate.decode()}}),
        encoding="utf-8",
    )
    capture_root = tmp_path / "captures"
    for index, relative in enumerate(check_no_secrets.CAPTURE_FILES):
        path = capture_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        header_value = har_candidate.decode() if index == 0 else "synthetic-har-value"
        path.write_text(
            json.dumps(
                {
                    "log": {
                        "entries": [
                            {
                                "request": {
                                    "url": "https://example.invalid/path",
                                    "headers": [
                                        {"name": "Authorization", "value": header_value}
                                    ],
                                }
                            }
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )
    _track(
        git_repository,
        "tracked.txt",
        private_candidate + b"\n" + har_candidate,
    )

    result = check_no_secrets.run(git_repository, git_repository, capture_root)
    output = capsys.readouterr()

    assert result == 1
    assert output.err == ""
    assert "private_sources=available" in output.out
    assert "hit_type=exact_private_value" in output.out
    assert private_candidate.decode() not in output.out
    assert har_candidate.decode() not in output.out


def test_secret_scanner_fails_closed_for_git_errors_and_missing_ignore_rules(
    git_repository: Any,
    tmp_path: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Catch Git failures falling back to a filesystem walk or incomplete ignore checks."""
    not_a_repository = tmp_path / "not-a-repository"
    not_a_repository.mkdir()

    assert check_no_secrets.run(not_a_repository, None, None) == 1
    git_output = capsys.readouterr()
    assert git_output.err == ""
    assert "hit_type=git_failure" in git_output.out
    assert str(not_a_repository) not in git_output.out

    (git_repository / ".gitignore").write_text(".private/\nlocal-evidence/\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(git_repository), "add", ".gitignore"],
        check=True,
        capture_output=True,
    )
    assert check_no_secrets.run(git_repository, None, None) == 1
    ignore_output = capsys.readouterr()
    assert ignore_output.err == ""
    assert "hit_type=missing_ignore_rule" in ignore_output.out
    assert "file=probe.pfx" in ignore_output.out
