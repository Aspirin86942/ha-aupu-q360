"""Behavior tests for Q360 discovery diffing and candidate reports."""

from __future__ import annotations

import importlib
from datetime import UTC, datetime

import pytest

from custom_components.aupu_q360.discovery_models import SanitizedValue


def _analysis():  # type: ignore[no-untyped-def]
    return importlib.import_module("custom_components.aupu_q360.discovery_analysis")


def _boolean(value: bool) -> SanitizedValue:
    return SanitizedValue(
        kind="boolean",
        comparison=value,
        public={"type": "boolean", "value": value},
    )


def _number(value: float) -> SanitizedValue:
    return SanitizedValue(
        kind="number",
        comparison=value,
        public={"type": "number", "value": value},
    )


def _timestamp(value: int) -> SanitizedValue:
    return SanitizedValue(
        kind="timestamp",
        comparison=value,
        public={"type": "timestamp", "precision": "seconds"},
    )


def _step(
    capability: str,
    target: str,
    round_number: int,
    *,
    before: dict[str, SanitizedValue] | None = None,
    after: dict[str, SanitizedValue] | None = None,
    baseline_restored: bool | None = None,
    invalid: bool = False,
    timed_out: bool = False,
):  # type: ignore[no-untyped-def]
    module = _analysis()
    return module.StepEvidence(
        label=module.StepLabel(capability, target, round_number),
        snapshot_succeeded=not timed_out,
        baseline_restored=baseline_restored,
        changes=module.diff_snapshots(before or {}, after or {}, ()),
        invalid=invalid,
        timed_out=timed_out,
    )


def _build_report(steps):  # type: ignore[no-untyped-def]
    return _analysis().build_discovery_report(
        integration_version="0.1.0",
        started_at=datetime(2026, 9, 2, 13, 47, tzinfo=UTC),
        wss_baseline_succeeded=True,
        steps=steps,
    )


def _heating_rounds(
    *,
    include_round_two: bool = True,
    include_off: bool = True,
    restore_round_two: bool = True,
    round_two_after: bool = True,
):
    path = "service/5/property/2"
    steps = [
        _step(
            "heating",
            "on",
            1,
            before={path: _boolean(False)},
            after={path: _boolean(True)},
        )
    ]
    if include_off:
        steps.append(
            _step(
                "heating",
                "off",
                1,
                before={path: _boolean(True)},
                after={path: _boolean(False)},
                baseline_restored=True,
            )
        )
    if include_round_two:
        steps.append(
            _step(
                "heating",
                "on",
                2,
                before={path: _boolean(False)},
                after={path: _boolean(round_two_after)},
            )
        )
        if include_off:
            steps.append(
                _step(
                    "heating",
                    "off",
                    2,
                    before={path: _boolean(True)},
                    after={path: _boolean(False)},
                    baseline_restored=restore_round_two,
                )
            )
    return steps


def test_diff_preserves_real_transients_without_counting_duplicate_updates() -> None:
    """Catch a momentary reported change disappearing when the final value recovers."""
    module = _analysis()
    path = "service/2/property/1"

    changes = module.diff_snapshots(
        {path: _boolean(False)},
        {path: _boolean(False)},
        (
            (path, _boolean(True)),
            (path, _boolean(True)),
            (path, _boolean(False)),
        ),
    )

    assert len(changes) == 1
    assert changes[0].to_public() == {
        "path": path,
        "data_type": "boolean",
        "direction": "changed",
        "before": {"type": "boolean", "value": False},
        "after": {"type": "boolean", "value": False},
        "transient_count": 2,
    }


def test_timestamp_diff_exposes_only_delta_and_direction() -> None:
    """Catch absolute Shadow timestamps leaking from a step report."""
    module = _analysis()
    path = "service/8/property/9"

    change = module.diff_snapshots(
        {path: _timestamp(1_700_000_000)},
        {path: _timestamp(1_700_000_030)},
        (),
    )[0]

    assert change.to_public() == {
        "path": path,
        "data_type": "timestamp",
        "direction": "increase",
        "delta": 30,
        "transient_count": 0,
    }
    assert "1700000000" not in repr(change)
    assert "1700000030" not in repr(change)


def test_two_consistent_rounds_with_two_baseline_restores_are_confirmed() -> None:
    """Catch a repeatable reported field failing to reach the review candidate list."""
    report = _build_report(_heating_rounds())

    assert report["candidates"][0] == {
        "capability": "heating",
        "path": "service/5/property/2",
        "data_type": "boolean",
        "classification": "confirmed_candidate",
        "evidence_steps": [
            "heating:on:1",
            "heating:off:1",
            "heating:on:2",
            "heating:off:2",
        ],
    }


@pytest.mark.parametrize(
    "steps",
    [
        pytest.param(_heating_rounds(include_round_two=False), id="one-round"),
        pytest.param(_heating_rounds(include_off=False), id="missing-off"),
        pytest.param(_heating_rounds(restore_round_two=False), id="baseline-not-restored"),
        pytest.param(_heating_rounds(round_two_after=False), id="inconsistent-rounds"),
    ],
)
def test_incomplete_or_inconsistent_evidence_is_ambiguous(steps) -> None:  # type: ignore[no-untyped-def]
    """Catch single-shot or non-recovering correlations becoming confirmed."""
    candidate = _build_report(steps)["candidates"][0]

    assert candidate["classification"] == "ambiguous"
    assert candidate["path"] == "service/5/property/2"


def test_path_shared_by_two_non_idle_capabilities_is_ambiguous() -> None:
    """Catch a common status field being assigned to whichever experiment ran first."""
    path = "service/5/property/2"
    steps = _heating_rounds()
    for round_number in (1, 2):
        steps.extend(
            (
                _step(
                    "ventilation",
                    "on",
                    round_number,
                    before={path: _boolean(False)},
                    after={path: _boolean(True)},
                ),
                _step(
                    "ventilation",
                    "off",
                    round_number,
                    before={path: _boolean(True)},
                    after={path: _boolean(False)},
                    baseline_restored=True,
                ),
            )
        )

    candidates = _build_report(steps)["candidates"]

    assert [candidate["classification"] for candidate in candidates[:2]] == [
        "ambiguous",
        "ambiguous",
    ]


def test_idle_numbers_are_unidentified_and_empty_capabilities_are_not_observed() -> None:
    """Catch an environmental number being named from range alone."""
    path = "service/7/property/3"
    report = _build_report(
        (
            _step(
                "idle_environment",
                "off",
                1,
                before={path: _number(21.0)},
                after={path: _number(21.5)},
                baseline_restored=False,
            ),
            _step("swing", "on", 1),
            _step("swing", "off", 1, baseline_restored=True),
            _step("swing", "on", 2),
            _step("swing", "off", 2, baseline_restored=True),
        )
    )

    candidates = report["candidates"]
    assert candidates[0] == {
        "capability": "swing",
        "path": None,
        "data_type": None,
        "classification": "not_observed",
        "evidence_steps": [],
    }
    assert candidates[1]["capability"] == "idle_environment"
    assert candidates[1]["path"] == path
    assert candidates[1]["classification"] == "observed_unidentified"


def test_invalid_step_prevents_candidate_upgrade() -> None:
    """Catch partial evidence from a resource-limited step becoming confirmed."""
    steps = _heating_rounds()
    steps[2] = _step("heating", "on", 2, invalid=True)

    candidate = _build_report(steps)["candidates"][0]

    assert candidate["classification"] == "invalid"


def test_report_schema_is_fixed_stable_and_counts_step_results() -> None:
    """Catch dynamic labels, absolute timestamps, or unbounded metadata entering reports."""
    steps = (
        _step("heating", "on", 1),
        _step("heating", "off", 1, invalid=True),
        _step("heating", "on", 2, timed_out=True),
    )

    report = _build_report(steps)

    assert set(report) == {
        "schema_version",
        "integration_version",
        "session_started_utc_hour",
        "wss_baseline_succeeded",
        "steps",
        "candidates",
        "limits",
        "statistics",
        "sanitization_scan",
    }
    assert report["session_started_utc_hour"] == "2026-09-02T13:00Z"
    assert report["limits"] == {
        "snapshot_timeout_seconds": 10,
        "step_timeout_seconds": 120,
        "session_timeout_seconds": 1200,
        "max_changes_per_step": 256,
        "mqtt_packet_bytes": 65536,
    }
    assert report["statistics"] == {
        "completed_steps": 1,
        "invalid_steps": 1,
        "timeouts": 1,
    }
    assert report["sanitization_scan"] == {"passed": True, "finding_count": 0}
    assert set(report["steps"][0]) == {
        "capability",
        "target",
        "round",
        "snapshot_succeeded",
        "baseline_restored",
        "invalid",
        "timed_out",
        "changes",
    }
