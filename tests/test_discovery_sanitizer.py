"""Behavior tests for in-memory Q360 discovery sanitization."""

from __future__ import annotations

import importlib
import math
import re
from datetime import UTC, datetime

import pytest


def _module():  # type: ignore[no-untyped-def]
    return importlib.import_module("custom_components.aupu_q360.discovery_sanitizer")


def _sanitizer():  # type: ignore[no-untyped-def]
    return _module().DiscoverySanitizer(
        session_key=b"s" * 32,
        device_id="123456789012345",
    )


def test_target_paths_are_aliased_before_values_leave_the_sanitizer() -> None:
    """Catch device identifiers or desired/sibling branches entering step evidence."""
    state = {
        "reported": {
            "123456789012345": {
                "2": {"properties": {"1": True}},
                "3": {"properties": {"7": 22.5}},
            },
            "999999999999999": {"8": {"properties": {"1": "private-sibling"}}},
        },
        "desired": {"123456789012345": {"9": {"properties": {"9": "private-desired"}}}},
    }

    snapshot = _sanitizer().sanitize_reported(state)

    assert set(snapshot) == {"service/2/property/1", "service/3/property/7"}
    assert snapshot["service/2/property/1"].public == {
        "type": "boolean",
        "value": True,
    }
    rendered = repr(snapshot)
    assert "123456789012345" not in rendered
    assert "999999999999999" not in rendered
    assert "private-sibling" not in rendered
    assert "private-desired" not in rendered


def test_scalar_values_are_sanitized_without_losing_comparison_semantics() -> None:
    """Catch raw strings/timestamps or bool-as-number confusion in evidence."""
    sanitizer = _sanitizer()
    state = {
        "reported": {
            "123456789012345": {
                "2": {
                    "properties": {
                        "1": True,
                        "2": 12.5,
                        "3": None,
                        "4": "private-text",
                        "5": 1_700_000_000,
                        "6": 1_700_000_000_000,
                    }
                }
            }
        }
    }

    snapshot = sanitizer.sanitize_reported(state)

    assert snapshot["service/2/property/1"].public == {
        "type": "boolean",
        "value": True,
    }
    assert snapshot["service/2/property/2"].public == {
        "type": "number",
        "value": 12.5,
    }
    assert snapshot["service/2/property/3"].public == {
        "type": "null",
        "occurrences": 1,
    }
    string_public = snapshot["service/2/property/4"].public
    assert string_public["type"] == "string"
    assert string_public["length"] == 12
    fingerprint = string_public["fingerprint"]
    assert isinstance(fingerprint, str)
    assert re.fullmatch(r"h-(?:[0-9a-f]{4}-){3}[0-9a-f]{4}", fingerprint)
    assert snapshot["service/2/property/5"].public == {
        "type": "timestamp",
        "precision": "seconds",
    }
    assert snapshot["service/2/property/6"].public == {
        "type": "timestamp",
        "precision": "milliseconds",
    }
    rendered = repr(snapshot)
    assert "private-text" not in rendered
    assert "1700000000" not in rendered


def test_string_fingerprints_are_stable_only_within_one_session() -> None:
    """Catch report values becoming reversible or correlatable across sessions."""
    state = {"reported": {"123456789012345": {"2": {"properties": {"1": "same-private-text"}}}}}
    first = _sanitizer().sanitize_reported(state)["service/2/property/1"]
    repeated = _sanitizer().sanitize_reported(state)["service/2/property/1"]
    other_session = (
        _module()
        .DiscoverySanitizer(
            session_key=b"o" * 32,
            device_id="123456789012345",
        )
        .sanitize_reported(state)["service/2/property/1"]
    )

    assert first.public["fingerprint"] == repeated.public["fingerprint"]
    assert first.public["fingerprint"] != other_session.public["fingerprint"]


def test_nested_values_keep_shape_but_not_keys_or_leaf_content() -> None:
    """Catch nested Shadow content being expanded into a report."""
    state = {
        "reported": {
            "123456789012345": {
                "2": {
                    "properties": {
                        "1": [1, {"private-key": "private-leaf"}],
                        "2": {"another-private-key": False},
                    }
                }
            }
        }
    }

    snapshot = _sanitizer().sanitize_reported(state)

    assert snapshot["service/2/property/1"].public == {
        "type": "array",
        "depth": 3,
        "elements": 4,
    }
    assert snapshot["service/2/property/2"].public == {
        "type": "object",
        "depth": 2,
        "elements": 2,
    }
    rendered = repr(snapshot)
    assert "private-key" not in rendered
    assert "private-leaf" not in rendered
    assert "another-private-key" not in rendered


@pytest.mark.parametrize(
    "state",
    [
        {"reported": {"123456789012345": {"x": {"properties": {"1": True}}}}},
        {"reported": {"123456789012345": {"12345678901": {"properties": {"1": True}}}}},
        {"reported": {"123456789012345": {"2": {"properties": []}}}},
        {"reported": {"123456789012345": {"2": {"properties": {"x": True}}}}},
        {"reported": {"123456789012345": {"2": {"properties": {"1": math.nan}}}}},
        {"reported": {"123456789012345": {"2": {"properties": {"1": math.inf}}}}},
        {"reported": {"123456789012345": {"2": {"properties": {"1": [[[[[1]]]]]}}}}},
        {"reported": {"123456789012345": {"2": {"properties": {"1": list(range(257))}}}}},
    ],
)
def test_invalid_target_structure_fails_with_one_fixed_error(state: object) -> None:
    """Catch malformed or resource-heavy target values becoming partial evidence."""
    module = _module()

    with pytest.raises(module.DiscoverySanitizationError) as raised:
        _sanitizer().sanitize_reported(state)

    assert str(raised.value) == "discovery_invalid_payload"


def test_report_scan_rejects_sensitive_markers_without_echoing_findings() -> None:
    """Catch a validated-looking report carrying identifiers or credential material."""
    module = _module()
    forbidden_value = "private-device-identifier"
    report = {"schema_version": 1, "steps": [{"path": forbidden_value}]}

    with pytest.raises(module.DiscoverySanitizationError) as raised:
        module.validate_discovery_report(report, forbidden_values=(forbidden_value,))

    assert str(raised.value) == "discovery_invalid_payload"
    assert forbidden_value not in repr(raised.value)


def test_report_scan_accepts_controlled_sanitized_content() -> None:
    """Catch final scanning rejecting the report's fixed public vocabulary."""
    from custom_components.aupu_q360.discovery_analysis import build_discovery_report

    result = _module().validate_discovery_report(
        build_discovery_report(
            integration_version="0.1.1",
            started_at=datetime(2026, 9, 2, 13, 47, tzinfo=UTC),
            wss_baseline_succeeded=True,
            steps=(),
        ),
        forbidden_values=("123456789012345", "synthetic-entry-id"),
    )

    assert result.passed is True
    assert result.finding_count == 0


@pytest.mark.parametrize(
    "mutation",
    [
        lambda report: report.update({"extra": True}),
        lambda report: report.update({"schema_version": 2}),
        lambda report: report["limits"].update({"step_timeout_seconds": 121}),
        lambda report: report["statistics"].update({"invalid_steps": -1}),
        lambda report: report["candidates"].append(
            {
                "capability": "unknown",
                "path": "service/2/property/1",
                "data_type": "boolean",
                "classification": "confirmed_candidate",
                "evidence_steps": [],
            }
        ),
    ],
)
def test_report_scan_rejects_non_fixed_schema(mutation) -> None:  # type: ignore[no-untyped-def]
    """Catch storage accepting arbitrary JSON merely because it has no obvious secret."""
    from custom_components.aupu_q360.discovery_analysis import build_discovery_report

    module = _module()
    report = build_discovery_report(
        integration_version="0.1.1",
        started_at=datetime(2026, 9, 2, 13, 47, tzinfo=UTC),
        wss_baseline_succeeded=True,
        steps=(),
    )
    mutation(report)

    with pytest.raises(module.DiscoverySanitizationError):
        module.validate_discovery_report(report, forbidden_values=())
