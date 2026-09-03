"""Tests for the temporary reported-only Q360 scalar probe."""

from __future__ import annotations

import pytest

from custom_components.aupu_q360.probe import (
    ProbeError,
    diff_probe_snapshots,
    extract_probe_snapshot,
)

DEVICE_ID = "123456789"


def test_extract_probe_snapshot_keeps_only_normalized_safe_scalars() -> None:
    state = {
        "desired": {DEVICE_ID: {"6": {"properties": {"2": 5}}}},
        "reported": {
            DEVICE_ID: {
                "5": {"properties": {"1": False, "2": "private-text"}},
                "6": {"properties": {"2": 4, "3": 1001, "4": 1.5}},
                "7": {"properties": {"1": [1], "2": {"nested": True}}},
            }
        },
    }

    assert extract_probe_snapshot(state, DEVICE_ID) == {
        "service/5/property/1": False,
        "service/6/property/2": 4,
    }


def test_diff_probe_snapshots_is_sorted_and_preserves_type_and_absence() -> None:
    before = {
        "service/5/property/1": False,
        "service/6/property/2": 0,
        "service/8/property/1": True,
    }
    after = {
        "service/5/property/1": 0,
        "service/6/property/2": 4,
        "service/7/property/2": 36,
    }

    assert [change.to_public() for change in diff_probe_snapshots(before, after)] == [
        {"path": "service/5/property/1", "before": False, "after": 0},
        {"path": "service/6/property/2", "before": 0, "after": 4},
        {"path": "service/7/property/2", "before": None, "after": 36},
        {"path": "service/8/property/1", "before": True, "after": None},
    ]


def test_probe_snapshot_rejects_invalid_target_structure_and_fixed_limits() -> None:
    with pytest.raises(ProbeError, match="probe_invalid_payload"):
        extract_probe_snapshot({"reported": {DEVICE_ID: []}}, DEVICE_ID)

    oversized = {
        "reported": {
            DEVICE_ID: {
                "6": {"properties": {str(index): index for index in range(1, 258)}}
            }
        }
    }
    with pytest.raises(ProbeError, match="probe_invalid_payload"):
        extract_probe_snapshot(oversized, DEVICE_ID)
