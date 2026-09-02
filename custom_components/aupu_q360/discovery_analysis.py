"""Stable diffing and candidate classification for Q360 state discovery."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from .discovery_models import (
    CandidateClassification,
    ChangeDirection,
    DiscoveryCapability,
    DiscoveryTarget,
    JsonObject,
    JsonValue,
    SanitizedChange,
    SanitizedValue,
    StepEvidence,
    StepLabel,
)

__all__ = [
    "StepEvidence",
    "StepLabel",
    "build_discovery_report",
    "diff_snapshots",
]

_CAPABILITY_ORDER = {capability: index for index, capability in enumerate(DiscoveryCapability)}


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


def build_discovery_report(
    *,
    integration_version: str,
    started_at: datetime,
    wss_baseline_succeeded: bool,
    steps: Sequence[StepEvidence],
) -> JsonObject:
    """Build the fixed, stable JSON report ready for the final safety scan."""
    ordered_steps = tuple(steps)
    candidates = _classify_candidates(ordered_steps)
    public_steps: list[JsonValue] = []
    public_steps.extend(step.to_public() for step in ordered_steps)
    public_candidates: list[JsonValue] = []
    public_candidates.extend(candidates)
    started_utc = started_at.astimezone(UTC)
    return {
        "schema_version": 1,
        "integration_version": integration_version,
        "session_started_utc_hour": started_utc.strftime("%Y-%m-%dT%H:00Z"),
        "wss_baseline_succeeded": wss_baseline_succeeded,
        "steps": public_steps,
        "candidates": public_candidates,
        "limits": {
            "snapshot_timeout_seconds": 10,
            "step_timeout_seconds": 120,
            "session_timeout_seconds": 1200,
            "max_changes_per_step": 256,
            "mqtt_packet_bytes": 65_536,
        },
        "statistics": {
            "completed_steps": sum(
                not step.invalid and not step.timed_out for step in ordered_steps
            ),
            "invalid_steps": sum(step.invalid for step in ordered_steps),
            "timeouts": sum(step.timed_out for step in ordered_steps),
        },
        "sanitization_scan": {"passed": True, "finding_count": 0},
    }


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
        and isinstance(after.comparison, (int, float))
    ):
        if after.comparison > before.comparison:
            return "increase"
        if after.comparison < before.comparison:
            return "decrease"
    return "changed"


def _classify_candidates(steps: Sequence[StepEvidence]) -> list[JsonObject]:
    by_capability: dict[DiscoveryCapability, list[StepEvidence]] = defaultdict(list)
    path_capabilities: dict[str, set[DiscoveryCapability]] = defaultdict(set)
    for step in steps:
        capability = step.label.capability
        by_capability[capability].append(step)
        if capability is DiscoveryCapability.IDLE_ENVIRONMENT:
            continue
        for change in step.changes:
            path_capabilities[change.path].add(capability)

    candidates: list[JsonObject] = []
    for capability in sorted(by_capability, key=lambda item: _CAPABILITY_ORDER[item]):
        capability_steps = by_capability[capability]
        paths = sorted({change.path for step in capability_steps for change in step.changes})
        capability_invalid = any(step.invalid or step.timed_out for step in capability_steps)
        if not paths:
            candidates.append(
                _candidate(
                    capability=capability,
                    path=None,
                    data_type=None,
                    classification="invalid" if capability_invalid else "not_observed",
                    evidence_steps=()
                    if not capability_invalid
                    else tuple(
                        step.label.evidence_id
                        for step in capability_steps
                        if step.invalid or step.timed_out
                    ),
                )
            )
            continue

        for path in paths:
            path_steps = [
                step
                for step in capability_steps
                if any(change.path == path for change in step.changes)
            ]
            first_change = next(
                change for step in path_steps for change in step.changes if change.path == path
            )
            evidence_steps = tuple(step.label.evidence_id for step in path_steps)
            classification: CandidateClassification
            if capability_invalid:
                classification = "invalid"
            elif capability is DiscoveryCapability.IDLE_ENVIRONMENT:
                classification = "observed_unidentified"
            elif len(path_capabilities[path]) > 1:
                classification = "ambiguous"
            else:
                confirmed_evidence = _confirmed_evidence(capability_steps, path)
                if confirmed_evidence is None:
                    classification = "ambiguous"
                else:
                    classification = "confirmed_candidate"
                    evidence_steps = confirmed_evidence
            candidates.append(
                _candidate(
                    capability=capability,
                    path=path,
                    data_type=first_change.data_type,
                    classification=classification,
                    evidence_steps=evidence_steps,
                )
            )
    return candidates


def _confirmed_evidence(steps: Sequence[StepEvidence], path: str) -> tuple[str, ...] | None:
    for target in DiscoveryTarget:
        if target is DiscoveryTarget.OFF:
            continue
        positive_steps: list[StepEvidence] = []
        off_steps: list[StepEvidence] = []
        for round_number in (1, 2):
            positive = _find_step(steps, target, round_number, path)
            off = _find_step(steps, DiscoveryTarget.OFF, round_number, path)
            if positive is None or off is None or off.baseline_restored is not True:
                break
            positive_steps.append(positive)
            off_steps.append(off)
        else:
            positive_changes = [_change_for_path(step, path) for step in positive_steps]
            off_changes = [_change_for_path(step, path) for step in off_steps]
            if (
                positive_changes[0].correlation_signature
                == positive_changes[1].correlation_signature
                and off_changes[0].correlation_signature == off_changes[1].correlation_signature
            ):
                return tuple(
                    evidence
                    for round_index in range(2)
                    for evidence in (
                        positive_steps[round_index].label.evidence_id,
                        off_steps[round_index].label.evidence_id,
                    )
                )
    return None


def _find_step(
    steps: Sequence[StepEvidence],
    target: DiscoveryTarget,
    round_number: int,
    path: str,
) -> StepEvidence | None:
    return next(
        (
            step
            for step in steps
            if step.label.target is target
            and step.label.round == round_number
            and step.snapshot_succeeded
            and not step.invalid
            and not step.timed_out
            and any(change.path == path for change in step.changes)
        ),
        None,
    )


def _change_for_path(step: StepEvidence, path: str) -> SanitizedChange:
    return next(change for change in step.changes if change.path == path)


def _candidate(
    *,
    capability: DiscoveryCapability,
    path: str | None,
    data_type: str | None,
    classification: CandidateClassification,
    evidence_steps: Sequence[str],
) -> JsonObject:
    return {
        "capability": capability.value,
        "path": path,
        "data_type": data_type,
        "classification": classification,
        "evidence_steps": list(evidence_steps),
    }
