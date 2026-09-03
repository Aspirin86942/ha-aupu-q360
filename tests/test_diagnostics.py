"""Tests for secret-free diagnostics and the repository secret scanner."""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from collections.abc import Coroutine, Generator
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from urllib.parse import quote

import pytest

from custom_components.aupu_q360.auth import BearerCredential
from custom_components.aupu_q360.coordinator import AupuCoordinator
from custom_components.aupu_q360.diagnostics import async_get_config_entry_diagnostics
from custom_components.aupu_q360.raw_discovery_archive import RawArchiveMetadata
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
    "state_discovery",
}


def _credential(expires_at: datetime | None) -> BearerCredential:
    segments = (
        "syntheticHeaderSegment",
        "syntheticPayloadSegment",
        "syntheticSignatureSegment",
    )
    token = ".".join(segments)
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
        "integration_version": "0.2.1",
        "authentication_expiry_bucket": ">=7d",
        "wss_enabled": True,
        "wss_connected": True,
        "wss_healthy": False,
        "last_error_code": "protocol_error",
        "light_state_source": "reported",
        "assumed_state": False,
        "state_discovery": {"report_available": False},
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
        "integration_version": "0.2.1",
        "authentication_expiry_bucket": "unknown",
        "wss_enabled": False,
        "wss_connected": False,
        "wss_healthy": False,
        "last_error_code": "none",
        "light_state_source": "unknown",
        "assumed_state": False,
        "state_discovery": {"report_available": False},
    }
    assert results == [expected, expected]
    assert all(secret not in json.dumps(result) for result in results)


def test_diagnostics_fold_secret_bearing_attribute_and_time_errors_to_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch property or expiry failures escaping into Home Assistant diagnostics logs."""
    secret = "synthetic-sensitive-exception-sentinel"

    class ExplodingEntry:
        @property
        def runtime_data(self) -> object:
            raise RuntimeError(secret)

    class ExplodingCoordinator:
        def __getattr__(self, name: str) -> object:
            del name
            raise RuntimeError(secret)

    class ExplodingDateTime(datetime):
        def __sub__(self, other: object) -> timedelta:
            del other
            raise RuntimeError(secret)

    class ExplodingRuntime:
        credential = _credential(ExplodingDateTime(2026, 9, 3, tzinfo=UTC))
        coordinator = ExplodingCoordinator()

        @property
        def use_wss(self) -> bool:
            raise RuntimeError(secret)

    monkeypatch.setattr("custom_components.aupu_q360.diagnostics._utcnow", lambda: _NOW)
    entries = (ExplodingEntry(), SimpleNamespace(runtime_data=ExplodingRuntime()))

    results = [
        _run(async_get_config_entry_diagnostics(None, cast(Any, entry))) for entry in entries
    ]

    expected = {
        "integration_version": "0.2.1",
        "authentication_expiry_bucket": "unknown",
        "wss_enabled": False,
        "wss_connected": False,
        "wss_healthy": False,
        "last_error_code": "none",
        "light_state_source": "unknown",
        "assumed_state": False,
        "state_discovery": {"report_available": False},
    }
    assert results == [expected, expected]
    assert all(secret not in json.dumps(result) for result in results)


def test_incomplete_runtime_does_not_compute_credential_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch a credential-only partial runtime exposing its expiry bucket."""
    monkeypatch.setattr("custom_components.aupu_q360.diagnostics._utcnow", lambda: _NOW)
    entry = SimpleNamespace(
        runtime_data=SimpleNamespace(
            credential=_credential(_NOW + timedelta(hours=1)),
            coordinator=None,
            use_wss=True,
        )
    )

    result = _run(async_get_config_entry_diagnostics(None, cast(Any, entry)))

    assert result["authentication_expiry_bucket"] == "unknown"
    assert set(result) == _DIAGNOSTIC_KEYS


def test_diagnostics_include_only_the_validated_latest_discovery_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch diagnostics reading Config Entry data or omitting the private report."""
    from custom_components.aupu_q360.discovery_analysis import build_discovery_report

    report = build_discovery_report(
        integration_version="0.2.0",
        started_at=datetime(2026, 9, 2, 13, 47, tzinfo=UTC),
        wss_baseline_succeeded=True,
        archive=RawArchiveMetadata(
            enabled=True,
            status="complete",
            session_id="rd-" + "a" * 32,
            event_count=12,
            file_bytes=3456,
            sha256="b" * 64,
        ),
        cycles=(),
    )

    class FakeReportStore:
        async def async_load(self) -> dict[str, Any]:
            return report

    sentinel = "private-config-entry-value"
    runtime = SimpleNamespace(
        credential=_credential(_NOW + timedelta(days=8)),
        use_wss=True,
        coordinator=SimpleNamespace(),
        discovery_store=FakeReportStore(),
        device=SimpleNamespace(did="123456789012345", tag="synthetic-device-tag"),
    )
    entry = SimpleNamespace(
        runtime_data=runtime,
        data={"secret": sentinel},
        entry_id="synthetic-entry-id",
    )
    monkeypatch.setattr("custom_components.aupu_q360.diagnostics._utcnow", lambda: _NOW)

    result = _run(async_get_config_entry_diagnostics(None, cast(Any, entry)))

    assert result["state_discovery"] == {
        "report_available": True,
        "report": report,
    }
    assert result["state_discovery"]["report"]["raw_archive"] == {
        "enabled": True,
        "status": "complete",
        "session_id": "rd-" + "a" * 32,
        "event_count": 12,
        "file_bytes": 3456,
        "sha256": "b" * 64,
    }
    assert sentinel not in json.dumps(result)
    assert "/var/lib/" not in json.dumps(result)
    assert "/home/george/" not in json.dumps(result)


def test_diagnostics_revalidate_fake_store_and_hide_raw_markers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Catch an invalid Store bypass injecting topics, payloads, paths, or identifiers."""
    from custom_components.aupu_q360.discovery_analysis import build_discovery_report

    sentinel = "synthetic-private-raw-marker"
    report = build_discovery_report(
        integration_version="0.2.0",
        started_at=datetime(2026, 9, 2, 13, 47, tzinfo=UTC),
        wss_baseline_succeeded=True,
        cycles=(),
    )
    invalid = deepcopy(report)
    invalid["raw_archive"] = {
        "enabled": True,
        "status": "complete",
        "session_id": "rd-" + "a" * 32,
        "event_count": 1,
        "file_bytes": 1,
        "sha256": "b" * 64,
        "path": f"/var/lib/{sentinel}",
        "topic": "$aws/things/123456789012345/shadow/get/accepted",
        "payload": sentinel,
    }

    class InvalidFakeStore:
        async def async_load(self) -> dict[str, Any]:
            return invalid

    runtime = SimpleNamespace(
        credential=_credential(_NOW + timedelta(days=8)),
        use_wss=True,
        coordinator=SimpleNamespace(),
        discovery_store=InvalidFakeStore(),
        device=SimpleNamespace(did="123456789012345", tag="synthetic-device-tag"),
    )
    entry = SimpleNamespace(runtime_data=runtime, entry_id="synthetic-entry-id")

    with caplog.at_level(logging.ERROR, logger="custom_components.aupu_q360.diagnostics"):
        result = _run(async_get_config_entry_diagnostics(None, cast(Any, entry)))

    assert result["state_discovery"] == {"report_available": False}
    serialized = json.dumps(result)
    assert sentinel not in serialized
    assert "$aws/things/" not in serialized
    assert "AUPU discovery diagnostics report rejected" in caplog.text
    assert sentinel not in caplog.text
    assert "$aws/things/" not in caplog.text


def test_diagnostics_downgrade_report_load_failure_without_exception_text() -> None:
    """Catch a corrupt Store failure escaping from the diagnostics endpoint."""
    sentinel = "private-store-exception"

    class FailingReportStore:
        async def async_load(self) -> None:
            raise RuntimeError(sentinel)

    runtime = SimpleNamespace(
        credential=_credential(_NOW + timedelta(days=8)),
        use_wss=True,
        coordinator=SimpleNamespace(),
        discovery_store=FailingReportStore(),
    )
    entry = SimpleNamespace(runtime_data=runtime, data={"secret": sentinel})

    result = _run(async_get_config_entry_diagnostics(None, cast(Any, entry)))

    assert result["state_discovery"] == {"report_available": False}
    assert sentinel not in json.dumps(result)


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
            coordinator=SimpleNamespace(),
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

    coordinator.async_apply_light_state(is_on=True)
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
            b"App-"
            + b"Authorization: app=production;ts=1760000000;signature="
            + b"QWxhZGRpbjpPcGVuU2VzYW1lU2lnbmF0dXJlMTIzNDU2Nzg5MA=="
        ),
        "assignment": (
            b"client_" + b"secret" + b' = "' + b"opaqueAssignmentValue9876543210" + b'"'
        ),
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


@pytest.mark.parametrize(
    "assignment_key",
    (b"secret", b"database_secret", b"signature", b"app_signature", b"jwt", b"jwt_token"),
)
def test_secret_scanner_detects_high_value_assignments_without_echoing_values(
    git_repository: Any,
    capsys: pytest.CaptureFixture[str],
    assignment_key: bytes,
) -> None:
    """Catch a generic high-value assignment key bypassing regex-only checkout scans."""
    sentinel = b"PureAlphabetic" + b"AssignmentSentinel"
    matching_line = assignment_key + b' = "' + sentinel + b'"'
    _track(git_repository, "assignment.txt", matching_line)

    result = check_no_secrets.run(git_repository, None, None)
    output = capsys.readouterr()

    assert result == 1
    assert output.err == ""
    assert "hit_type=assignment" in output.out
    assert sentinel.decode() not in output.out
    assert matching_line.decode() not in output.out


def test_secret_scanner_detects_unquoted_high_value_assignment(
    git_repository: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Catch an environment-style secret assignment bypassing quoted-value scans."""
    sentinel = b"UnquotedAlphabetic" + b"AssignmentSentinel"
    matching_line = b"DATABASE_" + b"SECRET=" + sentinel
    _track(git_repository, "environment.txt", matching_line)

    result = check_no_secrets.run(git_repository, None, None)
    output = capsys.readouterr()

    assert result == 1
    assert output.err == ""
    assert "hit_type=assignment" in output.out
    assert sentinel.decode() not in output.out
    assert matching_line.decode() not in output.out


@pytest.mark.parametrize(
    ("assignment_key", "value_label"),
    (
        (b"secret", b"Secret"),
        (b"signature", b"Signature"),
        (b"jwt", b"Jwt"),
    ),
    ids=("secret", "signature", "jwt"),
)
def test_secret_scanner_detects_unquoted_yaml_high_value_assignments(
    git_repository: Any,
    capsys: pytest.CaptureFixture[str],
    assignment_key: bytes,
    value_label: bytes,
) -> None:
    """Catch YAML high-value assignments without disclosing values or source lines."""
    sentinel = b"Synthetic" + value_label + b"Value0123456789"
    matching_line = assignment_key + b": " + sentinel + b"  # safe comment"
    _track(git_repository, "config.yml", matching_line)

    result = check_no_secrets.run(git_repository, None, None)
    output = capsys.readouterr()

    assert result == 1
    assert output.err == ""
    assert "hit_type=assignment" in output.out
    assert sentinel.decode() not in output.out
    assert matching_line.decode() not in output.out


def test_secret_scanner_ignores_yaml_type_annotations_short_values_and_trailing_prose(
    git_repository: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Keep YAML assignment matching bounded to high-value line-ending values."""
    high_looking_value = b"Synthetic" + b"HighLooking" + b"Value0123456789"
    content = b"\n".join(
        (
            b"secret: public",
            b"signature: str",
            b"jwt: HS256",
            b"secret: " + high_looking_value + b" trailing prose",
        )
    )
    _track(git_repository, "public.yml", content)

    result = check_no_secrets.run(git_repository, None, None)
    output = capsys.readouterr()

    assert result == 0
    assert output.err == ""
    assert "sensitive_hit_count=0" in output.out
    assert high_looking_value.decode() not in output.out


def test_secret_scanner_detects_encrypted_private_key_header_without_echoing_it(
    git_repository: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Catch a standard encrypted PKCS private key header bypassing the regex gate."""
    header = b"-----BEGIN " + b"ENCRYPTED PRIVATE KEY-----"
    _track(git_repository, "encrypted-key.txt", header)

    result = check_no_secrets.run(git_repository, None, None)
    output = capsys.readouterr()

    assert result == 1
    assert output.err == ""
    assert "hit_type=private_key" in output.out
    assert header.decode() not in output.out


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
        b"size = 139" + b"1234" + b"5678\nhash = abcdefghijk.lmnopqrstuv.wxyzABCDEFG\n",
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
                                    "headers": [{"name": "Authorization", "value": header_value}],
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


def _write_har_captures(capture_root: Any, request: dict[str, object]) -> None:
    for relative in check_no_secrets.CAPTURE_FILES:
        path = capture_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"log": {"entries": [{"request": request}]}}),
            encoding="utf-8",
        )


@pytest.mark.parametrize(
    "serialization",
    ("json_document", "url_query", "form_body"),
)
def test_low_information_private_candidate_matches_each_exact_document_context(
    git_repository: Any,
    tmp_path: Any,
    capsys: pytest.CaptureFixture[str],
    serialization: str,
) -> None:
    """Match low-information candidates only in exact structured contexts."""
    field_name = "deviceIdentifier"
    candidate = "SyntheticLowDevice/" + serialization
    capture_root = tmp_path / "low-candidate-captures"
    _write_har_captures(
        capture_root,
        {
            "url": "https://example.invalid/path",
            "postData": {
                "params": [{"name": field_name, "value": candidate}],
            },
        },
    )
    encoded_candidate = quote(candidate, safe="")
    serialized = {
        "json_document": json.dumps({field_name: candidate}).encode(),
        "url_query": (f"https://example.invalid/path?{field_name}={encoded_candidate}").encode(),
        "form_body": f"{field_name}={encoded_candidate}&public=value".encode(),
    }[serialization]
    _track(git_repository, "controlled-context.txt", serialized)

    result = check_no_secrets.run(git_repository, None, capture_root)
    output = capsys.readouterr()

    assert result == 1
    assert output.err == ""
    assert "private_sources=available" in output.out
    assert "hit_type=exact_private_value" in output.out
    assert candidate not in output.out
    assert serialized.decode() not in output.out


def test_low_information_form_requires_exact_parameter_name_and_value(
    git_repository: Any,
    tmp_path: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Reject form parameters whose full name or decoded value differs."""
    field_name = "deviceIdentifier"
    candidate = "SyntheticLowDevice/Exactness"
    capture_root = tmp_path / "low-candidate-captures"
    _write_har_captures(
        capture_root,
        {
            "url": "https://example.invalid/path",
            "postData": {
                "params": [{"name": field_name, "value": candidate}],
            },
        },
    )
    encoded_candidate = quote(candidate, safe="")
    mismatches = (
        f"other{field_name}={encoded_candidate}&{field_name}={encoded_candidate}Suffix"
    ).encode()
    _track(git_repository, "public-form.txt", mismatches)

    result = check_no_secrets.run(git_repository, None, capture_root)
    output = capsys.readouterr()

    assert result == 0
    assert output.err == ""
    assert "sensitive_hit_count=0" in output.out
    assert candidate not in output.out


def test_har_candidate_extraction_is_schema_aware_for_sensitive_value_aliases(
    tmp_path: Any,
) -> None:
    """Catch missing aliases and cookie metadata being treated as secret candidates."""
    values = {
        "signature": "PureAlphabetic" + "HarSignatureSentinel",
        "phone": "139" + "8642" + "7531",
        "device": "device-identifier-" + "synthetic",
        "cookie": "cookie-value-" + "synthetic",
        "jwt": "ShortJwt",
    }
    public_metadata = {
        "domain": "public-cookie-domain.invalid",
        "path": "/public-cookie-path",
    }
    request: dict[str, object] = {
        "url": "https://example.invalid/path",
        "headers": [{"name": "X-App-Sign", "value": values["signature"]}],
        "queryString": [{"name": "mobileNumber", "value": values["phone"]}],
        "cookies": [
            {
                "name": "route",
                "value": values["cookie"],
                **public_metadata,
            }
        ],
        "postData": {
            "params": [{"name": "deviceIdentifier", "value": values["device"]}],
            "text": json.dumps({"jwt": values["jwt"]}),
        },
    }
    capture_root = tmp_path / "schema-aware-captures"
    _write_har_captures(capture_root, request)
    captured = json.loads((capture_root / check_no_secrets.CAPTURE_FILES[0]).read_text())

    candidates = set(check_no_secrets._har_sensitive_strings(captured))
    classified, available = check_no_secrets._private_candidates(None, capture_root)
    by_value = {candidate.value: candidate for candidate in classified}

    assert set(values.values()) <= candidates
    assert set(public_metadata.values()).isdisjoint(candidates)
    assert available is True
    assert by_value[values["signature"]].source == "har_header"
    assert by_value[values["signature"]].sensitivity == "high"
    assert by_value[values["phone"]].source == "har_query"
    assert by_value[values["phone"]].sensitivity == "low"
    assert by_value[values["device"]].source == "har_parameter"
    assert by_value[values["device"]].sensitivity == "low"
    assert by_value[values["cookie"]].source == "har_cookie"
    assert by_value[values["cookie"]].sensitivity == "low"
    assert by_value[values["jwt"]].source == "har_json"
    assert by_value[values["jwt"]].sensitivity == "high"
    classified.clear()


@pytest.mark.parametrize("case", ("pure_alpha", "short_token", "json", "url"))
def test_exact_private_candidate_matches_each_controlled_serialization(
    git_repository: Any,
    capsys: pytest.CaptureFixture[str],
    case: str,
) -> None:
    """Catch one high-value raw, short, JSON, or URL form being omitted."""
    cases = {
        "pure_alpha": (
            "app_key",
            "PureAlphabetic" + "SignerSentinel",
            lambda value: value.encode(),
        ),
        "short_token": ("token", "ShortTok", lambda value: value.encode()),
        "json": (
            "key_prefix",
            "JsonQuote" + '"' + "Slash" + "\\" + "Sentinel",
            lambda value: json.dumps(value).encode(),
        ),
        "url": (
            "key_suffix",
            "Url Value/" + "SignerSentinel",
            lambda value: quote(value, safe="").encode(),
        ),
    }
    field_name, candidate, serialize = cases[case]
    private_file = git_repository / ".private" / "signer_secrets.json"
    private_file.parent.mkdir(parents=True)
    private_file.write_text(
        json.dumps({field_name: candidate}),
        encoding="utf-8",
    )
    tracked = b"serialized-field=" + serialize(candidate)
    _track(git_repository, "serialized-values.txt", tracked)

    result = check_no_secrets.run(git_repository, git_repository, None)
    output = capsys.readouterr()

    assert result == 1
    assert output.err == ""
    assert "hit_type=exact_private_value" in output.out
    assert candidate not in output.out
    assert tracked.decode() not in output.out


def test_low_information_private_candidate_does_not_match_public_prose(
    git_repository: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Catch low-information signer format text reverting to arbitrary substring scans."""
    low_information = "formatlabel"
    private_file = git_repository / ".private" / "signer_secrets.json"
    private_file.parent.mkdir(parents=True)
    private_file.write_text(
        json.dumps({"sdk_label": low_information}),
        encoding="utf-8",
    )
    _track(
        git_repository,
        "public-prose.txt",
        ("prefix-" + low_information + "-suffix").encode(),
    )

    result = check_no_secrets.run(git_repository, git_repository, None)
    output = capsys.readouterr()

    assert result == 0
    assert output.err == ""
    assert "sensitive_hit_count=0" in output.out
    assert low_information not in output.out


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
