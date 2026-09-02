"""Behavior tests for in-memory Q360 discovery sanitization."""

from __future__ import annotations

import importlib
import json
import math
import re
from datetime import UTC, datetime

import pytest


def _module():  # type: ignore[no-untyped-def]
    return importlib.import_module("custom_components.aupu_q360.discovery_sanitizer")


def _sanitizer(*, session_key: bytes = b"s" * 32):  # type: ignore[no-untyped-def]
    return _module().DiscoverySanitizer(
        session_key=session_key,
        device_id="123456789012345",
    )


def _value(value: object, *, session_key: bytes = b"s" * 32):  # type: ignore[no-untyped-def]
    return _sanitizer(session_key=session_key).sanitize_reported(
        {"reported": {"123456789012345": {"2": {"properties": {"1": value}}}}}
    )["service/2/property/1"]


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


@pytest.mark.parametrize("number", (-1000, -12.5, 0, 1000))
def test_bounded_numbers_remain_direct(number: float) -> None:
    """Catch ordinary bounded measurements becoming unusable for value mapping."""
    sanitized = _value(number)

    assert sanitized.public == {"type": "number", "value": number}
    assert sanitized.comparison == number


@pytest.mark.parametrize("number", (-1001, 1001, -1000.5, 1000.5, 10**30))
def test_large_numbers_use_session_only_fingerprints(number: float) -> None:
    """Catch large non-timestamp numbers entering public evidence as raw values."""
    first = _value(number)
    repeated = _value(number)
    other_session = _value(number, session_key=b"o" * 32)

    fingerprint = first.public["fingerprint"]
    assert first.public == {
        "type": "number",
        "representation": "fingerprint",
        "fingerprint": fingerprint,
    }
    assert isinstance(fingerprint, str)
    assert re.fullmatch(r"h-(?:[0-9a-f]{4}-){3}[0-9a-f]{4}", fingerprint)
    assert first.comparison == repeated.comparison
    assert first.comparison != other_session.comparison
    assert str(number) not in repr(first)


def test_large_integer_and_float_keep_explicit_json_type_semantics() -> None:
    """Catch numerically equal integer and float encodings collapsing into one signature."""
    assert _value(1001).comparison != _value(1001.0).comparison


def test_timestamp_ranges_take_precedence_over_large_number_fingerprinting() -> None:
    """Catch recognized seconds or milliseconds timestamps being treated as public numbers."""
    assert _value(1_700_000_000).public == {
        "type": "timestamp",
        "precision": "seconds",
    }
    assert _value(1_700_000_000_000).public == {
        "type": "timestamp",
        "precision": "milliseconds",
    }


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


def test_nested_values_keep_content_fingerprints_but_not_keys_or_leaves() -> None:
    """Catch nested Shadow content being exposed or reduced to a shape-only comparison."""
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

    array_public = snapshot["service/2/property/1"].public
    assert array_public == {
        "type": "array",
        "depth": 3,
        "elements": 4,
        "fingerprint": array_public["fingerprint"],
    }
    object_public = snapshot["service/2/property/2"].public
    assert object_public == {
        "type": "object",
        "depth": 2,
        "elements": 2,
        "fingerprint": object_public["fingerprint"],
    }
    rendered = repr(snapshot)
    assert "private-key" not in rendered
    assert "private-leaf" not in rendered
    assert "another-private-key" not in rendered


def test_canonical_objects_ignore_insertion_order_but_compare_all_content() -> None:
    """Catch object insertion order changing identity or changed content retaining identity."""
    first = _value({"alpha": [1, True], "beta": {"leaf": "value"}})
    reordered = _value({"beta": {"leaf": "value"}, "alpha": [1, True]})
    changed_key = _value({"alpha": [1, True], "gamma": {"leaf": "value"}})
    changed_leaf = _value({"alpha": [1, True], "beta": {"leaf": "other"}})
    changed_bool = _value({"alpha": [1, False], "beta": {"leaf": "value"}})
    changed_number = _value({"alpha": [2, True], "beta": {"leaf": "value"}})

    assert first.comparison == reordered.comparison
    assert first.public["fingerprint"] == reordered.public["fingerprint"]
    assert (
        len(
            {
                first.comparison,
                changed_key.comparison,
                changed_leaf.comparison,
                changed_bool.comparison,
                changed_number.comparison,
            }
        )
        == 5
    )


def test_canonical_arrays_compare_order_even_when_shape_is_identical() -> None:
    """Catch reordered array values collapsing into one shape-only comparison."""
    first = _value([1, "two", False])
    reordered = _value([False, "two", 1])

    assert first.public["depth"] == reordered.public["depth"]
    assert first.public["elements"] == reordered.public["elements"]
    assert first.comparison != reordered.comparison


def test_container_fingerprints_are_unlinkable_across_sessions() -> None:
    """Catch canonical container content becoming correlatable between sessions."""
    value = {"private-key": ["private-leaf", 7]}
    first = _value(value)
    repeated = _value(value)
    other_session = _value(value, session_key=b"o" * 32)

    assert first.comparison == repeated.comparison
    assert first.comparison != other_session.comparison
    rendered = json.dumps(first.public, sort_keys=True)
    assert "private-key" not in rendered
    assert "private-leaf" not in rendered


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
        {"reported": {"123456789012345": {"2": {"properties": {"1": {1: "value"}}}}}},
        {"reported": {"123456789012345": {"2": {"properties": {"1": {"x": {1}}}}}}},
        {"reported": {"123456789012345": {"2": {"properties": {"1": [math.nan]}}}}},
        {"reported": {"123456789012345": {"2": {"properties": {"1": {"x" * 65: "value"}}}}}},
        {"reported": {"123456789012345": {"2": {"properties": {"1": "x" * 65_537}}}}},
    ],
)
def test_invalid_target_structure_fails_with_one_fixed_error(state: object) -> None:
    """Catch malformed or resource-heavy target values becoming partial evidence."""
    module = _module()

    with pytest.raises(module.DiscoverySanitizationError) as raised:
        _sanitizer().sanitize_reported(state)

    assert str(raised.value) == "discovery_invalid_payload"


def test_packet_sized_string_boundary_is_accepted() -> None:
    """Catch the fixed MQTT packet-derived string boundary rejecting its legal endpoint."""
    sanitized = _value("x" * 65_536)

    assert sanitized.public["type"] == "string"
    assert sanitized.public["length"] == 65_536


def test_close_clears_the_key_and_rejects_all_later_sanitization() -> None:
    """Catch completed sessions retaining a usable HMAC key or accepting new evidence."""
    module = _module()
    sanitizer = _sanitizer()
    sanitizer.sanitize_reported(
        {"reported": {"123456789012345": {"2": {"properties": {"1": "value"}}}}}
    )

    sanitizer.close()
    sanitizer.close()

    assert sanitizer._session_key is None
    with pytest.raises(module.DiscoverySanitizationError) as raised:
        sanitizer.sanitize_reported(
            {"reported": {"123456789012345": {"2": {"properties": {"1": "value"}}}}}
        )
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
    from custom_components.aupu_q360.raw_discovery_archive import RawArchiveMetadata

    result = _module().validate_discovery_report(
        build_discovery_report(
            integration_version="0.2.0",
            started_at=datetime(2026, 9, 2, 13, 47, tzinfo=UTC),
            wss_baseline_succeeded=True,
            archive=RawArchiveMetadata.not_requested(),
            cycles=(),
        ),
        forbidden_values=("123456789012345", "synthetic-entry-id"),
    )

    assert result.passed is True
    assert result.finding_count == 0


@pytest.mark.parametrize(
    "mutation",
    [
        lambda report: report.update({"extra": True}),
        lambda report: report.update({"schema_version": 1}),
        lambda report: report["limits"].update({"stage_timeout_seconds": 121}),
        lambda report: report["statistics"].update({"invalid_cycles": -1}),
        lambda report: report["candidates"].append(
            {
                "experiment": "unknown",
                "role": "mode",
                "path": "service/2/property/1",
                "data_type": "boolean",
                "classification": "confirmed_candidate",
                "association": "dedicated",
                "value_mappings": [],
                "evidence_cycles": [],
            }
        ),
    ],
)
def test_report_scan_rejects_non_fixed_schema(mutation) -> None:  # type: ignore[no-untyped-def]
    """Catch storage accepting arbitrary JSON merely because it has no obvious secret."""
    from custom_components.aupu_q360.discovery_analysis import build_discovery_report
    from custom_components.aupu_q360.raw_discovery_archive import RawArchiveMetadata

    module = _module()
    report = build_discovery_report(
        integration_version="0.2.0",
        started_at=datetime(2026, 9, 2, 13, 47, tzinfo=UTC),
        wss_baseline_succeeded=True,
        archive=RawArchiveMetadata.not_requested(),
        cycles=(),
    )
    mutation(report)

    with pytest.raises(module.DiscoverySanitizationError):
        module.validate_discovery_report(report, forbidden_values=())
