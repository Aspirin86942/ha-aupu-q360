"""Stable phase diffing, restoration, coverage, and v2 candidate analysis."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from .discovery_catalog import EXPERIMENT_CATALOG, definition_for
from .discovery_models import (
    CandidateClassification,
    ChangeDirection,
    CycleEvidence,
    DiscoveryCoverage,
    DiscoveryExperiment,
    DiscoveryPhase,
    ExperimentKind,
    JsonObject,
    JsonValue,
    PathRestoration,
    RestorationResult,
    SanitizedChange,
    SanitizedValue,
)
from .raw_discovery_archive import RawArchiveMetadata

type CandidateRole = Literal["mode", "parameter", "carrier", "idle"]

_EXPERIMENT_ORDER = {experiment: index for index, experiment in enumerate(EXPERIMENT_CATALOG)}
_ROLE_ORDER: dict[CandidateRole, int] = {
    "mode": 0,
    "parameter": 1,
    "carrier": 2,
    "idle": 3,
}
_POSITIVE_PHASES = frozenset(
    {
        DiscoveryPhase.MODE_ON,
        DiscoveryPhase.CARRIER_ON,
        DiscoveryPhase.PARAMETER_CHANGE,
    }
)


@dataclass(slots=True)
class _Candidate:
    experiment: DiscoveryExperiment
    role: CandidateRole
    path: str | None
    data_type: str | None
    classification: CandidateClassification
    association: Literal["dedicated", "shared"] | None
    value_mappings: list[JsonObject]
    evidence_cycles: tuple[str, ...]
    signature: tuple[object, ...] | None = None
    carrier: DiscoveryExperiment | None = None

    def to_public(self) -> JsonObject:
        public: JsonObject = {
            "experiment": self.experiment.value,
            "role": self.role,
            "path": self.path,
            "data_type": self.data_type,
            "classification": self.classification,
            "association": self.association,
            "value_mappings": [dict(item) for item in self.value_mappings],
            "evidence_cycles": list(self.evidence_cycles),
        }
        if self.carrier is not None:
            public["carrier"] = self.carrier.value
        return public


def diff_snapshots(
    before: Mapping[str, SanitizedValue],
    after: Mapping[str, SanitizedValue],
    transient: Sequence[tuple[str, SanitizedValue]],
) -> tuple[SanitizedChange, ...]:
    """Return deterministic final and momentary changes between safe snapshots."""
    transient_by_path: dict[str, list[SanitizedValue]] = defaultdict(list)
    for path, value in transient:
        transient_by_path[path].append(value)

    changes: list[SanitizedChange] = []
    paths = sorted(set(before) | set(after) | set(transient_by_path))
    for path in paths:
        before_value = before.get(path)
        after_value = after.get(path)
        transient_count = _count_transient_changes(before_value, transient_by_path.get(path, ()))
        if _values_equal(before_value, after_value) and transient_count == 0:
            continue
        data_type = (
            after_value.kind
            if after_value is not None
            else before_value.kind
            if before_value is not None
            else transient_by_path[path][-1].kind
        )
        changes.append(
            SanitizedChange(
                path=path,
                data_type=data_type,
                direction=_change_direction(before_value, after_value),
                before=before_value,
                after=after_value,
                transient_count=transient_count,
            )
        )
    return tuple(changes)


def evaluate_restoration(
    *,
    reference: Mapping[str, SanitizedValue],
    positive_changes: Sequence[SanitizedChange],
    candidate: Mapping[str, SanitizedValue],
    background: Collection[str],
    confirmed_paths: Collection[str],
) -> RestorationResult:
    """Evaluate only paths changed by the corresponding positive phase."""
    relevant_paths = tuple(
        sorted(
            {
                change.path
                for change in positive_changes
                if change.path not in background and change.data_type != "timestamp"
            }
        )
    )
    restorations = tuple(
        PathRestoration(
            path=path,
            restored=_values_equal(reference.get(path), candidate.get(path)),
        )
        for path in relevant_paths
    )
    restored_paths = frozenset(item.path for item in restorations if item.restored)
    unrestored_paths = frozenset(item.path for item in restorations if not item.restored)
    confirmed_relevant = frozenset(relevant_paths) & frozenset(confirmed_paths)
    required = bool(relevant_paths) and (
        bool(confirmed_relevant & unrestored_paths)
        or (not confirmed_relevant and not restored_paths)
    )
    return RestorationResult(
        restorations=restorations,
        restored_paths=restored_paths,
        unrestored_paths=unrestored_paths,
        required=required,
    )


def background_paths(cycles: Sequence[CycleEvidence]) -> frozenset[str]:
    """Return timestamps plus paths repeated only in both completed idle rounds."""
    timestamps = {
        change.path
        for cycle in cycles
        for phase in cycle.phases
        for change in phase.changes
        if change.data_type == "timestamp"
    }
    idle_by_round: dict[int, set[str]] = defaultdict(set)
    non_idle_positive: set[str] = set()
    for cycle in cycles:
        if not _usable_cycle(cycle):
            continue
        for phase in cycle.phases:
            if cycle.request.experiment is DiscoveryExperiment.IDLE_ENVIRONMENT:
                if phase.phase is DiscoveryPhase.IDLE_OBSERVATION:
                    idle_by_round[cycle.request.round].update(
                        change.path for change in phase.changes if change.data_type != "timestamp"
                    )
            elif phase.phase in _POSITIVE_PHASES:
                non_idle_positive.update(change.path for change in phase.changes)
    repeated_idle = idle_by_round.get(1, set()) & idle_by_round.get(2, set())
    return frozenset(timestamps | (repeated_idle - non_idle_positive))


def confirmed_paths_for_experiment(
    cycles: Sequence[CycleEvidence],
    experiment: DiscoveryExperiment | str,
) -> frozenset[str]:
    """Return repeatable and reversible paths currently confirmed for one experiment."""
    controlled = DiscoveryExperiment(experiment)
    candidates = _classify_candidates(cycles)
    return frozenset(
        candidate.path
        for candidate in candidates
        if candidate.experiment is controlled
        and candidate.path is not None
        and candidate.classification == "confirmed_candidate"
    )


def build_discovery_report(
    *,
    integration_version: str,
    started_at: datetime,
    wss_baseline_succeeded: bool,
    archive: RawArchiveMetadata | None = None,
    cycles: Sequence[CycleEvidence] = (),
) -> JsonObject:
    """Build a deterministic schema-v2 report from sanitized phase evidence."""
    ordered_cycles = tuple(
        sorted(
            cycles,
            key=lambda cycle: (
                _EXPERIMENT_ORDER[cycle.request.experiment],
                cycle.request.cycle_id,
            ),
        )
    )
    coverage = _coverage_rows(ordered_cycles)
    candidates = _classify_candidates(ordered_cycles)
    started_utc = started_at.astimezone(UTC)
    public_cycles: list[JsonValue] = [cycle.to_public() for cycle in ordered_cycles]
    public_candidates: list[JsonValue] = [candidate.to_public() for candidate in candidates]
    public_coverage: list[JsonValue] = list(coverage)
    return {
        "schema_version": 2,
        "integration_version": integration_version,
        "session_started_utc_hour": started_utc.strftime("%Y-%m-%dT%H:00Z"),
        "wss_baseline_succeeded": wss_baseline_succeeded,
        "raw_archive": (archive or RawArchiveMetadata.not_requested()).to_public(),
        "coverage": public_coverage,
        "cycles": public_cycles,
        "candidates": public_candidates,
        "limits": {
            "snapshot_timeout_seconds": 10,
            "stage_timeout_seconds": 120,
            "session_timeout_seconds": 3600,
            "max_changes_per_phase": 256,
            "mqtt_packet_bytes": 65_536,
            "raw_archive_bytes": 64 * 1024 * 1024,
        },
        "statistics": {
            "completed_cycles": sum(_usable_cycle(cycle) for cycle in ordered_cycles),
            "invalid_cycles": sum(
                cycle.invalid or any(phase.invalid for phase in cycle.phases)
                for cycle in ordered_cycles
            ),
            "timeouts": sum(cycle.timed_out for cycle in ordered_cycles),
            "restore_failures": sum(cycle.restoration_failure_count for cycle in ordered_cycles),
        },
        "sanitization_scan": {"passed": True, "finding_count": 0},
    }


def _coverage_rows(cycles: Sequence[CycleEvidence]) -> list[JsonObject]:
    rows: list[JsonObject] = []
    for experiment in EXPERIMENT_CATALOG:
        relevant = tuple(cycle for cycle in cycles if cycle.request.experiment is experiment)
        usable = tuple(cycle for cycle in relevant if _usable_cycle(cycle))
        required = 8 if experiment is DiscoveryExperiment.GLOBAL_FAN_LEVEL else 2
        completed = len({cycle.request.cycle_id for cycle in usable})
        status = DiscoveryCoverage.NOT_STARTED
        if relevant:
            status = DiscoveryCoverage.PARTIAL
        if _coverage_complete(experiment, usable):
            status = DiscoveryCoverage.COMPLETE
        rows.append(
            {
                "experiment": experiment.value,
                "status": status.value,
                "required_rounds": required,
                "completed_rounds": completed,
            }
        )
    return rows


def _coverage_complete(
    experiment: DiscoveryExperiment,
    cycles: Sequence[CycleEvidence],
) -> bool:
    if experiment is DiscoveryExperiment.GLOBAL_FAN_LEVEL:
        sources = {cycle.request.source_level for cycle in cycles}
        if len(sources) != 1 or None in sources:
            return False
        source = next(iter(sources))
        expected = {
            (target, round_number)
            for target in (1, 2, 3, 4, 5)
            if target != source
            for round_number in (1, 2)
        }
        observed = {(cycle.request.target_level, cycle.request.round) for cycle in cycles}
        return observed == expected
    if experiment is DiscoveryExperiment.AI_TARGET_TEMPERATURE:
        pairs = {
            (cycle.request.source_temperature, cycle.request.target_temperature) for cycle in cycles
        }
        return len(pairs) == 1 and {cycle.request.round for cycle in cycles} == {1, 2}
    return {cycle.request.round for cycle in cycles} == {1, 2}


def _classify_candidates(cycles: Sequence[CycleEvidence]) -> list[_Candidate]:
    background = background_paths(cycles)
    candidates: list[_Candidate] = []
    for experiment in EXPERIMENT_CATALOG:
        definition = definition_for(experiment)
        relevant = tuple(cycle for cycle in cycles if cycle.request.experiment is experiment)
        usable = tuple(cycle for cycle in relevant if _usable_cycle(cycle))
        complete = _coverage_complete(experiment, usable)
        invalid = any(
            cycle.invalid
            or cycle.timed_out
            or any(phase.invalid or phase.timed_out for phase in cycle.phases)
            for cycle in relevant
        )
        if invalid:
            candidates.append(
                _empty_candidate(
                    experiment,
                    _primary_role(definition.kind),
                    "invalid",
                    relevant,
                )
            )
            continue
        if definition.kind is ExperimentKind.IDLE:
            candidates.extend(_idle_candidates(experiment, usable, complete))
            continue
        primary_role: CandidateRole = (
            "mode" if definition.kind is ExperimentKind.MODE else "parameter"
        )
        candidates.extend(
            _role_candidates(
                experiment=experiment,
                role=primary_role,
                cycles=usable,
                complete=complete,
                background=background,
            )
        )
        if definition.kind is ExperimentKind.PARAMETER:
            candidates.extend(
                _role_candidates(
                    experiment=experiment,
                    role="carrier",
                    cycles=usable,
                    complete=complete,
                    background=background,
                    carrier=definition.carrier,
                )
            )
    _resolve_associations(candidates)
    return sorted(
        candidates,
        key=lambda candidate: (
            _EXPERIMENT_ORDER[candidate.experiment],
            _ROLE_ORDER[candidate.role],
            "" if candidate.path is None else candidate.path,
            candidate.evidence_cycles,
        ),
    )


def _idle_candidates(
    experiment: DiscoveryExperiment,
    cycles: Sequence[CycleEvidence],
    complete: bool,
) -> list[_Candidate]:
    by_path: dict[str, list[SanitizedChange]] = defaultdict(list)
    evidence: dict[str, set[str]] = defaultdict(set)
    for cycle in cycles:
        for phase in cycle.phases:
            if phase.phase is not DiscoveryPhase.IDLE_OBSERVATION:
                continue
            for change in phase.changes:
                by_path[change.path].append(change)
                evidence[change.path].add(cycle.request.cycle_id)
    if not by_path:
        return [_empty_candidate(experiment, "idle", "not_observed", cycles)] if complete else []
    return [
        _Candidate(
            experiment=experiment,
            role="idle",
            path=path,
            data_type=changes[0].data_type,
            classification="observed_unidentified",
            association=None,
            value_mappings=[],
            evidence_cycles=tuple(sorted(evidence[path])),
        )
        for path, changes in sorted(by_path.items())
    ]


def _role_candidates(
    *,
    experiment: DiscoveryExperiment,
    role: CandidateRole,
    cycles: Sequence[CycleEvidence],
    complete: bool,
    background: Collection[str],
    carrier: DiscoveryExperiment | None = None,
) -> list[_Candidate]:
    positive_phase, restore_phase = _role_phases(role)
    paths = sorted(
        {
            change.path
            for cycle in cycles
            for phase in cycle.phases
            if phase.phase is positive_phase
            for change in phase.changes
            if change.path not in background and change.data_type != "timestamp"
        }
    )
    if not paths:
        if complete and role != "carrier":
            return [_empty_candidate(experiment, role, "not_observed", cycles)]
        return []

    result: list[_Candidate] = []
    for path in paths:
        changes: list[tuple[CycleEvidence, SanitizedChange]] = []
        restored = True
        for cycle in cycles:
            change = _change_for_phase(cycle, positive_phase, path)
            if change is None:
                continue
            changes.append((cycle, change))
            restoration = _restoration_for_phase(cycle, restore_phase, path)
            if restoration is not True:
                restored = False
        mappings, mapping_signature, mapping_valid = _value_mappings(
            experiment,
            role,
            changes,
            carrier,
        )
        all_cycles_observed = len(changes) == len(cycles) and bool(cycles)
        repeatable = _changes_repeatable(experiment, changes)
        confirmed = complete and all_cycles_observed and restored and repeatable and mapping_valid
        result.append(
            _Candidate(
                experiment=experiment,
                role=role,
                path=path,
                data_type=changes[0][1].data_type,
                classification="confirmed_candidate" if confirmed else "ambiguous",
                association=None,
                value_mappings=mappings,
                evidence_cycles=tuple(sorted(cycle.request.cycle_id for cycle, _ in changes)),
                signature=mapping_signature if confirmed else None,
                carrier=carrier,
            )
        )
    return result


def _value_mappings(
    experiment: DiscoveryExperiment,
    role: CandidateRole,
    changes: Sequence[tuple[CycleEvidence, SanitizedChange]],
    carrier: DiscoveryExperiment | None,
) -> tuple[list[JsonObject], tuple[object, ...], bool]:
    values: dict[str, tuple[SanitizedValue, JsonObject]] = {}
    valid = True
    for cycle, change in changes:
        if change.before is None or change.after is None:
            valid = False
            continue
        source_label, target_label = _mapping_labels(cycle, role, carrier)
        for label, value in ((source_label, change.before), (target_label, change.after)):
            existing = values.get(label)
            if existing is not None and not _values_equal(existing[0], value):
                valid = False
            else:
                values[label] = (value, dict(value.public))
    ordered_labels = sorted(values, key=_mapping_label_order)
    comparisons = [values[label][0] for label in ordered_labels]
    if len({(value.kind, value.comparison) for value in comparisons}) != len(comparisons):
        valid = False
    mappings: list[JsonObject] = [
        {"label": label, "value": dict(values[label][1])} for label in ordered_labels
    ]
    signature: tuple[object, ...] = tuple(
        (label, values[label][0].kind, values[label][0].comparison) for label in ordered_labels
    )
    return mappings, signature, valid


def _mapping_labels(
    cycle: CycleEvidence,
    role: CandidateRole,
    carrier: DiscoveryExperiment | None,
) -> tuple[str, str]:
    if role == "mode":
        return "off", "on"
    if role == "carrier":
        if carrier is None:
            raise ValueError("carrier label required")
        return "off", carrier.value
    if cycle.request.experiment is DiscoveryExperiment.GLOBAL_FAN_LEVEL:
        return f"level_{cycle.request.source_level}", f"level_{cycle.request.target_level}"
    return (
        f"temperature_{cycle.request.source_temperature}",
        f"temperature_{cycle.request.target_temperature}",
    )


def _mapping_label_order(label: str) -> tuple[int, int | str]:
    if label == "off":
        return (0, 0)
    if label == "on":
        return (1, 0)
    if label.startswith("level_"):
        return (2, int(label.removeprefix("level_")))
    if label.startswith("temperature_"):
        return (3, int(label.removeprefix("temperature_")))
    return (4, label)


def _changes_repeatable(
    experiment: DiscoveryExperiment,
    changes: Sequence[tuple[CycleEvidence, SanitizedChange]],
) -> bool:
    grouped: dict[object, list[tuple[object, ...]]] = defaultdict(list)
    for cycle, change in changes:
        key: object = (
            cycle.request.target_level
            if experiment is DiscoveryExperiment.GLOBAL_FAN_LEVEL
            else "single"
        )
        grouped[key].append(change.correlation_signature)
    return bool(grouped) and all(
        len(signatures) == 2 and signatures[0] == signatures[1] for signatures in grouped.values()
    )


def _resolve_associations(candidates: Sequence[_Candidate]) -> None:
    by_path: dict[str, list[_Candidate]] = defaultdict(list)
    for candidate in candidates:
        if candidate.path is not None and candidate.role != "idle":
            by_path[candidate.path].append(candidate)
    for path_candidates in by_path.values():
        if len(path_candidates) == 1:
            candidate = path_candidates[0]
            if candidate.classification == "confirmed_candidate":
                candidate.association = "dedicated"
            continue
        signatures = [candidate.signature for candidate in path_candidates]
        distinguishable = (
            all(candidate.classification == "confirmed_candidate" for candidate in path_candidates)
            and all(signature is not None for signature in signatures)
            and len(set(signatures)) == len(signatures)
        )
        if distinguishable:
            for candidate in path_candidates:
                candidate.association = "shared"
        else:
            for candidate in path_candidates:
                candidate.classification = "ambiguous"
                candidate.association = None


def _empty_candidate(
    experiment: DiscoveryExperiment,
    role: CandidateRole,
    classification: Literal["not_observed", "invalid"],
    cycles: Sequence[CycleEvidence],
) -> _Candidate:
    return _Candidate(
        experiment=experiment,
        role=role,
        path=None,
        data_type=None,
        classification=classification,
        association=None,
        value_mappings=[],
        evidence_cycles=tuple(sorted(cycle.request.cycle_id for cycle in cycles)),
    )


def _primary_role(kind: ExperimentKind) -> CandidateRole:
    if kind is ExperimentKind.MODE:
        return "mode"
    if kind is ExperimentKind.PARAMETER:
        return "parameter"
    return "idle"


def _role_phases(role: CandidateRole) -> tuple[DiscoveryPhase, DiscoveryPhase]:
    if role == "mode":
        return DiscoveryPhase.MODE_ON, DiscoveryPhase.MODE_RESTORE
    if role == "parameter":
        return DiscoveryPhase.PARAMETER_CHANGE, DiscoveryPhase.PARAMETER_RESTORE
    if role == "carrier":
        return DiscoveryPhase.CARRIER_ON, DiscoveryPhase.CARRIER_OFF
    raise ValueError("idle phases do not use restoration")


def _change_for_phase(
    cycle: CycleEvidence,
    phase_name: DiscoveryPhase,
    path: str,
) -> SanitizedChange | None:
    return next(
        (
            change
            for phase in cycle.phases
            if phase.phase is phase_name and not phase.invalid and not phase.timed_out
            for change in phase.changes
            if change.path == path
        ),
        None,
    )


def _restoration_for_phase(
    cycle: CycleEvidence,
    phase_name: DiscoveryPhase,
    path: str,
) -> bool | None:
    return next(
        (
            restoration.restored
            for phase in reversed(cycle.phases)
            if phase.phase is phase_name and not phase.invalid and not phase.timed_out
            for restoration in phase.restorations
            if restoration.path == path
        ),
        None,
    )


def _usable_cycle(cycle: CycleEvidence) -> bool:
    return (
        cycle.completed
        and not cycle.invalid
        and not cycle.timed_out
        and not any(phase.invalid for phase in cycle.phases)
    )


def _count_transient_changes(
    initial: SanitizedValue | None,
    values: Sequence[SanitizedValue],
) -> int:
    current = initial
    count = 0
    for value in values:
        if not _values_equal(current, value):
            count += 1
            current = value
    return count


def _values_equal(
    left: SanitizedValue | None,
    right: SanitizedValue | None,
) -> bool:
    if left is None or right is None:
        return left is right
    return left.kind == right.kind and left.comparison == right.comparison


def _change_direction(
    before: SanitizedValue | None,
    after: SanitizedValue | None,
) -> ChangeDirection:
    if before is None:
        return "added"
    if after is None:
        return "removed"
    if before.kind == after.kind == "boolean":
        if before.comparison is False and after.comparison is True:
            return "off_to_on"
        if before.comparison is True and after.comparison is False:
            return "on_to_off"
    if (
        before.kind in ("number", "timestamp")
        and after.kind == before.kind
        and isinstance(before.comparison, (int, float))
        and not isinstance(before.comparison, bool)
        and isinstance(after.comparison, (int, float))
        and not isinstance(after.comparison, bool)
    ):
        if after.comparison > before.comparison:
            return "increase"
        if after.comparison < before.comparison:
            return "decrease"
    return "changed"
