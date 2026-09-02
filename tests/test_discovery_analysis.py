"""Behavior tests for Q360 v2 phase evidence, analysis, and sanitized reports."""

from __future__ import annotations

import copy
from datetime import UTC, datetime

import pytest

from custom_components.aupu_q360.discovery_analysis import (
    background_paths,
    build_discovery_report,
    confirmed_paths_for_experiment,
    diff_snapshots,
    evaluate_restoration,
)
from custom_components.aupu_q360.discovery_catalog import build_step_request
from custom_components.aupu_q360.discovery_models import (
    CycleEvidence,
    DiscoveryExperiment,
    DiscoveryPhase,
    PathRestoration,
    PhaseEvidence,
    SanitizedValue,
)
from custom_components.aupu_q360.discovery_report_schema import validate_discovery_report
from custom_components.aupu_q360.discovery_sanitizer import DiscoverySanitizationError
from custom_components.aupu_q360.raw_discovery_archive import RawArchiveMetadata

_PATH = "service/5/property/2"
_OTHER_PATH = "service/5/property/3"
_TIMESTAMP_PATH = "service/9/property/1"


def _boolean(value: bool) -> SanitizedValue:
    return SanitizedValue(
        kind="boolean",
        comparison=value,
        public={"type": "boolean", "value": value},
    )


def _number(value: int) -> SanitizedValue:
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


def _phase(
    phase: DiscoveryPhase,
    *,
    before: dict[str, SanitizedValue] | None = None,
    after: dict[str, SanitizedValue] | None = None,
    restorations: tuple[PathRestoration, ...] = (),
    attempt: int = 1,
    invalid: bool = False,
    timed_out: bool = False,
) -> PhaseEvidence:
    return PhaseEvidence(
        phase=phase,
        attempt=attempt,
        snapshot_succeeded=not timed_out,
        changes=diff_snapshots(before or {}, after or {}, ()),
        restorations=restorations,
        invalid=invalid,
        timed_out=timed_out,
    )


def _mode_cycle(
    experiment: str,
    round_number: int,
    *,
    path: str = _PATH,
    before: SanitizedValue | None = None,
    after: SanitizedValue | None = None,
    restored: bool = True,
    completed: bool = True,
    invalid: bool = False,
) -> CycleEvidence:
    before_value = before or _boolean(False)
    after_value = after or _boolean(True)
    positive_before = {} if path == "" else {path: before_value}
    positive_after = {} if path == "" else {path: after_value}
    restorations = () if path == "" else (PathRestoration(path=path, restored=restored),)
    return CycleEvidence(
        request=build_step_request(experiment=experiment, round_number=round_number),
        phases=(
            _phase(
                DiscoveryPhase.MODE_ON,
                before=positive_before,
                after=positive_after,
                invalid=invalid,
            ),
            _phase(
                DiscoveryPhase.MODE_RESTORE,
                before=positive_after,
                after=positive_before,
                restorations=restorations,
                invalid=invalid,
            ),
        ),
        completed=completed,
        invalid=invalid,
    )


def _idle_cycle(
    round_number: int,
    *,
    path: str = _OTHER_PATH,
    before: SanitizedValue | None = None,
    after: SanitizedValue | None = None,
) -> CycleEvidence:
    return CycleEvidence(
        request=build_step_request(experiment="idle_environment", round_number=round_number),
        phases=(
            _phase(
                DiscoveryPhase.IDLE_OBSERVATION,
                before={path: before or _number(1)},
                after={path: after or _number(2)},
            ),
        ),
    )


def _parameter_cycle(
    experiment: str,
    round_number: int,
    source: int,
    target: int,
    *,
    path: str = _PATH,
    restored: bool = True,
    invalid: bool = False,
) -> CycleEvidence:
    if experiment == "global_fan_level":
        request = build_step_request(
            experiment=experiment,
            round_number=round_number,
            source_level=source,
            target_level=target,
        )
    else:
        request = build_step_request(
            experiment=experiment,
            round_number=round_number,
            source_temperature=source,
            target_temperature=target,
        )
    before = {path: _number(source)}
    after = {path: _number(target)}
    return CycleEvidence(
        request=request,
        phases=(
            _phase(DiscoveryPhase.CARRIER_ON),
            _phase(
                DiscoveryPhase.PARAMETER_CHANGE,
                before=before,
                after=after,
                invalid=invalid,
            ),
            _phase(
                DiscoveryPhase.PARAMETER_RESTORE,
                before=after,
                after=before,
                restorations=(PathRestoration(path=path, restored=restored),),
                invalid=invalid,
            ),
            _phase(DiscoveryPhase.CARRIER_OFF),
        ),
        invalid=invalid,
    )


def _report(
    cycles: tuple[CycleEvidence, ...] = (),
    *,
    archive: RawArchiveMetadata | None = None,
) -> dict[str, object]:
    return build_discovery_report(
        integration_version="0.2.0",
        started_at=datetime(2026, 9, 3, 0, 47, tzinfo=UTC),
        wss_baseline_succeeded=True,
        archive=archive or RawArchiveMetadata.not_requested(),
        cycles=cycles,
    )


def _coverage(report: dict[str, object], experiment: str) -> dict[str, object]:
    rows = report["coverage"]
    assert isinstance(rows, list)
    return next(row for row in rows if row["experiment"] == experiment)


def _candidates(report: dict[str, object], experiment: str) -> list[dict[str, object]]:
    rows = report["candidates"]
    assert isinstance(rows, list)
    return [row for row in rows if row["experiment"] == experiment]


def test_diff_preserves_transients_and_timestamp_delta_without_absolute_time() -> None:
    """Catch transient changes disappearing or absolute timestamps entering public evidence."""
    transient = diff_snapshots(
        {_PATH: _boolean(False)},
        {_PATH: _boolean(False)},
        ((_PATH, _boolean(True)), (_PATH, _boolean(True)), (_PATH, _boolean(False))),
    )[0]
    timestamp = diff_snapshots(
        {_TIMESTAMP_PATH: _timestamp(1_700_000_000)},
        {_TIMESTAMP_PATH: _timestamp(1_700_000_030)},
        (),
    )[0]

    assert transient.to_public() == {
        "path": _PATH,
        "data_type": "boolean",
        "direction": "changed",
        "before": {"type": "boolean", "value": False},
        "after": {"type": "boolean", "value": False},
        "transient_count": 2,
    }
    assert timestamp.to_public() == {
        "path": _TIMESTAMP_PATH,
        "data_type": "timestamp",
        "direction": "increase",
        "delta": 30,
        "transient_count": 0,
    }
    assert "1700000000" not in repr(timestamp.to_public())


def test_restoration_compares_only_positive_nonbackground_paths() -> None:
    """Catch whole-document equality or unrelated environmental drift blocking restoration."""
    reference = {
        _PATH: _boolean(False),
        _OTHER_PATH: _number(20),
        _TIMESTAMP_PATH: _timestamp(1_700_000_000),
    }
    positive = diff_snapshots(
        reference,
        {
            _PATH: _boolean(True),
            _OTHER_PATH: _number(21),
            _TIMESTAMP_PATH: _timestamp(1_700_000_010),
        },
        (),
    )
    candidate = {
        _PATH: _boolean(False),
        _OTHER_PATH: _number(99),
        _TIMESTAMP_PATH: _timestamp(1_700_000_020),
    }

    result = evaluate_restoration(
        reference=reference,
        positive_changes=positive,
        candidate=candidate,
        background={_OTHER_PATH, _TIMESTAMP_PATH},
        confirmed_paths=frozenset(),
    )

    assert result.required is False
    assert result.restored_paths == frozenset({_PATH})
    assert result.unrestored_paths == frozenset()
    assert result.restorations == (PathRestoration(path=_PATH, restored=True),)


def test_first_observation_and_confirmed_path_restoration_rules() -> None:
    """Catch first-cycle ambiguity being stricter or confirmed-path failures being weaker."""
    reference = {_PATH: _boolean(False), _OTHER_PATH: _boolean(False)}
    positive = diff_snapshots(
        reference,
        {_PATH: _boolean(True), _OTHER_PATH: _boolean(True)},
        (),
    )
    partial = {_PATH: _boolean(False), _OTHER_PATH: _boolean(True)}

    first = evaluate_restoration(
        reference=reference,
        positive_changes=positive,
        candidate=partial,
        background=frozenset(),
        confirmed_paths=frozenset(),
    )
    confirmed = evaluate_restoration(
        reference=reference,
        positive_changes=positive,
        candidate=partial,
        background=frozenset(),
        confirmed_paths=frozenset({_OTHER_PATH}),
    )
    all_unrestored = evaluate_restoration(
        reference=reference,
        positive_changes=positive,
        candidate={_PATH: _boolean(True), _OTHER_PATH: _boolean(True)},
        background=frozenset(),
        confirmed_paths=frozenset(),
    )
    none = evaluate_restoration(
        reference=reference,
        positive_changes=(),
        candidate=partial,
        background=frozenset(),
        confirmed_paths=frozenset({_PATH}),
    )

    assert first.required is False
    assert first.restored_paths == frozenset({_PATH})
    assert first.unrestored_paths == frozenset({_OTHER_PATH})
    assert confirmed.required is True
    assert all_unrestored.required is True
    assert none.required is False


def test_background_paths_require_two_idle_rounds_and_no_positive_experiment() -> None:
    """Catch one-off or experiment-correlated paths being mislabeled background."""
    idle_one = _idle_cycle(1)
    idle_two = _idle_cycle(2)
    timestamp_mode = _mode_cycle(
        "night_light",
        1,
        path=_TIMESTAMP_PATH,
        before=_timestamp(1_700_000_000),
        after=_timestamp(1_700_000_010),
    )

    assert background_paths((idle_one,)) == frozenset()
    assert background_paths((idle_one, idle_two, timestamp_mode)) == frozenset(
        {_OTHER_PATH, _TIMESTAMP_PATH}
    )

    mode_on_same_path = _mode_cycle("night_light", 2, path=_OTHER_PATH)
    assert background_paths((idle_one, idle_two, mode_on_same_path)) == frozenset()


def test_mode_coverage_and_confirmed_paths_require_two_reversible_rounds() -> None:
    """Catch partial or non-restored mode evidence being called complete and confirmed."""
    first = _mode_cycle("night_light", 1)
    second = _mode_cycle("night_light", 2)
    broken = _mode_cycle("night_light", 2, restored=False)

    assert _coverage(_report(), "night_light")["status"] == "not_started"
    assert _coverage(_report((first,)), "night_light")["status"] == "partial"
    complete = _report((second, first))
    assert _coverage(complete, "night_light") == {
        "experiment": "night_light",
        "status": "complete",
        "required_rounds": 2,
        "completed_rounds": 2,
    }
    assert confirmed_paths_for_experiment(
        (first, second), DiscoveryExperiment.NIGHT_LIGHT
    ) == frozenset({_PATH})
    assert (
        confirmed_paths_for_experiment((first, broken), DiscoveryExperiment.NIGHT_LIGHT)
        == frozenset()
    )


def test_fan_coverage_requires_one_source_all_other_targets_and_both_rounds() -> None:
    """Catch three-level or mixed-source evidence satisfying five-level coverage."""
    complete = tuple(
        _parameter_cycle("global_fan_level", round_number, 3, target)
        for target in (1, 2, 4, 5)
        for round_number in (1, 2)
    )
    mixed_source = complete[:-1] + (_parameter_cycle("global_fan_level", 2, 2, 5),)

    assert _coverage(_report(complete), "global_fan_level") == {
        "experiment": "global_fan_level",
        "status": "complete",
        "required_rounds": 8,
        "completed_rounds": 8,
    }
    assert _coverage(_report(complete[:-1]), "global_fan_level")["status"] == "partial"
    assert _coverage(_report(mixed_source), "global_fan_level")["status"] == "partial"

    candidate = _candidates(_report(complete), "global_fan_level")[0]
    assert candidate["classification"] == "confirmed_candidate"
    assert candidate["role"] == "parameter"
    assert candidate["association"] == "dedicated"
    assert candidate["value_mappings"] == [
        {"label": "level_1", "value": {"type": "number", "value": 1}},
        {"label": "level_2", "value": {"type": "number", "value": 2}},
        {"label": "level_3", "value": {"type": "number", "value": 3}},
        {"label": "level_4", "value": {"type": "number", "value": 4}},
        {"label": "level_5", "value": {"type": "number", "value": 5}},
    ]


def test_temperature_coverage_requires_the_same_adjacent_pair_twice() -> None:
    """Catch changed or reversed temperature experiments satisfying repeatability."""
    first = _parameter_cycle("ai_target_temperature", 1, 35, 36)
    second = _parameter_cycle("ai_target_temperature", 2, 35, 36)
    reversed_pair = _parameter_cycle("ai_target_temperature", 2, 36, 35)

    assert _coverage(_report((first, second)), "ai_target_temperature")["status"] == "complete"
    assert (
        _coverage(_report((first, reversed_pair)), "ai_target_temperature")["status"] == "partial"
    )
    candidate = _candidates(_report((first, second)), "ai_target_temperature")[0]
    assert candidate["value_mappings"] == [
        {"label": "temperature_35", "value": {"type": "number", "value": 35}},
        {"label": "temperature_36", "value": {"type": "number", "value": 36}},
    ]


def test_candidate_classifications_distinguish_empty_partial_idle_and_invalid() -> None:
    """Catch incomplete coverage becoming not-observed or idle/invalid evidence being confirmed."""
    empty_complete = (
        _mode_cycle("night_light", 1, path=""),
        _mode_cycle("night_light", 2, path=""),
    )
    partial_empty = (_mode_cycle("ventilation", 1, path=""),)
    invalid = (
        _mode_cycle("air_blowing", 1, invalid=True),
        _mode_cycle("air_blowing", 2),
    )
    report = _report((*empty_complete, *partial_empty, *invalid, _idle_cycle(1), _idle_cycle(2)))

    assert _candidates(report, "night_light")[0]["classification"] == "not_observed"
    assert _candidates(report, "ventilation") == []
    assert _candidates(report, "air_blowing")[0]["classification"] == "invalid"
    idle = _candidates(report, "idle_environment")[0]
    assert idle["classification"] == "observed_unidentified"
    assert idle["path"] == _OTHER_PATH


def test_shared_paths_require_pairwise_distinguishable_repeatable_signatures() -> None:
    """Catch shared enum paths being rejected or indistinguishable signatures being confirmed."""
    distinguishable = (
        _mode_cycle("night_light", 1, before=_number(0), after=_number(1)),
        _mode_cycle("night_light", 2, before=_number(0), after=_number(1)),
        _mode_cycle("ventilation", 1, before=_number(0), after=_number(2)),
        _mode_cycle("ventilation", 2, before=_number(0), after=_number(2)),
    )
    report = _report(distinguishable)
    night = _candidates(report, "night_light")[0]
    ventilation = _candidates(report, "ventilation")[0]
    assert (night["classification"], night["association"]) == (
        "confirmed_candidate",
        "shared",
    )
    assert (ventilation["classification"], ventilation["association"]) == (
        "confirmed_candidate",
        "shared",
    )

    duplicate = distinguishable[:2] + (
        _mode_cycle("ventilation", 1, before=_number(0), after=_number(1)),
        _mode_cycle("ventilation", 2, before=_number(0), after=_number(1)),
    )
    duplicate_report = _report(duplicate)
    assert all(
        candidate["classification"] == "ambiguous"
        for candidate in (
            _candidates(duplicate_report, "night_light")[0],
            _candidates(duplicate_report, "ventilation")[0],
        )
    )


@pytest.mark.parametrize("kind", ("string", "number", "array", "object"))
def test_candidate_mappings_publish_only_hmac_representations(kind: str) -> None:
    """Catch fingerprinted Shadow values being dropped or exposed as raw candidate data."""
    before_fingerprint = "h-1111-2222-3333-4444"
    after_fingerprint = "h-aaaa-bbbb-cccc-dddd"
    if kind == "string":
        before_public = {"type": "string", "length": 8, "fingerprint": before_fingerprint}
        after_public = {"type": "string", "length": 9, "fingerprint": after_fingerprint}
    elif kind == "number":
        before_public = {
            "type": "number",
            "representation": "fingerprint",
            "fingerprint": before_fingerprint,
        }
        after_public = {
            "type": "number",
            "representation": "fingerprint",
            "fingerprint": after_fingerprint,
        }
    else:
        before_public = {
            "type": kind,
            "depth": 2,
            "elements": 3,
            "fingerprint": before_fingerprint,
        }
        after_public = {
            "type": kind,
            "depth": 2,
            "elements": 3,
            "fingerprint": after_fingerprint,
        }
    before = SanitizedValue(
        kind=kind,  # type: ignore[arg-type]
        comparison=(kind, before_fingerprint),
        public=before_public,
    )
    after = SanitizedValue(
        kind=kind,  # type: ignore[arg-type]
        comparison=(kind, after_fingerprint),
        public=after_public,
    )
    report = _report(
        (
            _mode_cycle("night_light", 1, before=before, after=after),
            _mode_cycle("night_light", 2, before=before, after=after),
        )
    )
    candidate = _candidates(report, "night_light")[0]

    assert candidate["classification"] == "confirmed_candidate"
    assert candidate["value_mappings"] == [
        {"label": "off", "value": before_public},
        {"label": "on", "value": after_public},
    ]
    assert validate_discovery_report(report, forbidden_values=()).passed is True


def test_report_has_exact_v2_top_level_limits_statistics_and_deterministic_order() -> None:
    """Catch schema drift, unstable ordering, or recomputed counts diverging from cycles."""
    cycles = (
        _mode_cycle("night_light", 2),
        _mode_cycle("night_light", 1),
        _parameter_cycle("ai_target_temperature", 1, 35, 36, invalid=True),
    )
    archive = RawArchiveMetadata(
        enabled=True,
        status="complete",
        session_id="rd-" + "a" * 32,
        event_count=7,
        file_bytes=1234,
        sha256="b" * 64,
    )
    report = _report(cycles, archive=archive)

    assert set(report) == {
        "schema_version",
        "integration_version",
        "session_started_utc_hour",
        "wss_baseline_succeeded",
        "raw_archive",
        "coverage",
        "cycles",
        "candidates",
        "limits",
        "statistics",
        "sanitization_scan",
    }
    assert report["schema_version"] == 2
    assert report["session_started_utc_hour"] == "2026-09-03T00:00Z"
    assert report["raw_archive"] == {
        "enabled": True,
        "status": "complete",
        "session_id": "rd-" + "a" * 32,
        "event_count": 7,
        "file_bytes": 1234,
        "sha256": "b" * 64,
    }
    assert report["limits"] == {
        "snapshot_timeout_seconds": 10,
        "stage_timeout_seconds": 120,
        "session_timeout_seconds": 3600,
        "max_changes_per_phase": 256,
        "mqtt_packet_bytes": 65_536,
        "raw_archive_bytes": 64 * 1024 * 1024,
    }
    assert report["statistics"] == {
        "completed_cycles": 2,
        "invalid_cycles": 1,
        "timeouts": 0,
        "restore_failures": 0,
    }
    assert report["sanitization_scan"] == {"passed": True, "finding_count": 0}
    assert report == _report(tuple(reversed(cycles)), archive=archive)
    assert validate_discovery_report(report, forbidden_values=()).passed is True


@pytest.mark.parametrize(
    "mutation",
    (
        lambda report: report.update({"extra": True}),
        lambda report: report.update({"schema_version": 1}),
        lambda report: report["limits"].update({"stage_timeout_seconds": 121}),
        lambda report: report["statistics"].update({"completed_cycles": 99}),
        lambda report: report["coverage"][0].update({"required_rounds": 99}),
        lambda report: report["candidates"][0].update({"association": "shared"}),
        lambda report: report["candidates"][0]["value_mappings"][0].update(
            {"label": "raw-private-label"}
        ),
        lambda report: report.update(
            {
                "raw_archive": {
                    "enabled": True,
                    "status": "complete",
                    "session_id": "rd-" + "a" * 32,
                    "event_count": 1,
                    "file_bytes": 1,
                    "sha256": "b" * 64,
                    "path": "/var/lib/private",
                }
            }
        ),
    ),
)
def test_schema_rejects_structure_count_and_archive_mutations(mutation) -> None:  # type: ignore[no-untyped-def]
    """Catch a merely JSON-serializable object bypassing the exact v2 schema."""
    report = _report((_mode_cycle("night_light", 1), _mode_cycle("night_light", 2)))
    mutation(report)

    with pytest.raises(DiscoverySanitizationError) as raised:
        validate_discovery_report(report, forbidden_values=())

    assert str(raised.value) == "discovery_invalid_payload"


@pytest.mark.parametrize(
    "marker",
    (
        "$aws/things/",
        "clientToken",
        "raw-payload-marker",
        "raw-topic-marker",
        "raw-desired-marker",
        "Bearer synthetic-sensitive-marker",
        "/home/george/private",
        "/var/lib/aupu-q360-private-discovery",
        "synthetic-device-identifier",
    ),
)
def test_final_scan_rejects_sensitive_markers_without_echoing_findings(marker: str) -> None:
    """Catch raw identifiers, transport content, or paths entering a validated report."""
    report = _report()
    report["integration_version"] = marker

    with pytest.raises(DiscoverySanitizationError) as raised:
        validate_discovery_report(
            report,
            forbidden_values=("synthetic-device-identifier",),
        )

    assert str(raised.value) == "discovery_invalid_payload"
    assert marker not in repr(raised.value)


def test_phase_public_output_contains_only_controlled_labels_and_sanitized_values() -> None:
    """Catch comparison values, retry internals, or free text escaping phase evidence."""
    phase = _phase(
        DiscoveryPhase.MODE_RESTORE,
        before={_PATH: _boolean(True)},
        after={_PATH: _boolean(False)},
        restorations=(PathRestoration(path=_PATH, restored=True),),
        attempt=2,
    )

    assert phase.to_public() == {
        "phase": "mode_restore",
        "attempt": 2,
        "snapshot_succeeded": True,
        "invalid": False,
        "timed_out": False,
        "changes": [
            {
                "path": _PATH,
                "data_type": "boolean",
                "direction": "on_to_off",
                "before": {"type": "boolean", "value": True},
                "after": {"type": "boolean", "value": False},
                "transient_count": 0,
            }
        ],
        "restorations": [{"path": _PATH, "restored": True}],
    }


def test_report_mutation_does_not_modify_original_cycle_evidence() -> None:
    """Catch report JSON sharing mutable structures with immutable in-memory evidence."""
    cycle = _mode_cycle("night_light", 1)
    report = _report((cycle,))
    cloned = copy.deepcopy(report)
    cloned["cycles"][0]["phases"][0]["changes"][0]["after"]["value"] = False

    assert _report((cycle,)) == report
