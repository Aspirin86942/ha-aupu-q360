"""Exact schema-v2 validation and final sensitive-content scan."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from typing import cast

from .discovery_catalog import EXPERIMENT_CATALOG, build_step_request, definition_for
from .discovery_models import (
    DiscoveryCoverage,
    DiscoveryExperiment,
    DiscoveryPhase,
    ExperimentKind,
    JsonObject,
    JsonValue,
    ScanResult,
)
from .discovery_sanitizer import DiscoverySanitizationError

_PATH = re.compile(r"service/[0-9]{1,10}/property/[0-9]{1,10}")
_HMAC = re.compile(r"h-(?:[0-9a-f]{4}-){3}[0-9a-f]{4}")
_SESSION_ID = re.compile(r"rd-[0-9a-f]{32}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SESSION_HOUR = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:00Z")
_INTEGRATION_VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,31}")
_JWT = re.compile(r"[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
_PHONE = re.compile(r"1[3-9][0-9]{9}")
_ABSOLUTE_PRIVATE_PATH = re.compile(r"/(?:home|var|config|root|tmp)/", re.IGNORECASE)
_FORBIDDEN_MARKERS = (
    "$aws/things/",
    "bearer ",
    "clienttoken",
    "payload",
    "topic",
    "desired",
    "authorization",
    "signer",
    "private key",
)
_TOP_LEVEL_KEYS = {
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
_INVARIANT_LIMITS = {
    "snapshot_timeout_seconds": 10,
    "max_changes_per_phase": 256,
    "mqtt_packet_bytes": 65_536,
    "raw_archive_bytes": 64 * 1024 * 1024,
}
_TIMEOUT_PROFILES = frozenset({(120, 3600), (300, 3300)})
_LIMIT_KEYS = {*_INVARIANT_LIMITS, "stage_timeout_seconds", "session_timeout_seconds"}
_VALUE_KINDS = frozenset({"boolean", "number", "null", "string", "timestamp", "object", "array"})
_DIRECTIONS = frozenset(
    {"added", "removed", "increase", "decrease", "off_to_on", "on_to_off", "changed"}
)
_CLASSIFICATIONS = frozenset(
    {"confirmed_candidate", "ambiguous", "observed_unidentified", "not_observed", "invalid"}
)
_ROLES = frozenset({"mode", "parameter", "carrier", "idle"})


def validate_discovery_report(
    report: object,
    *,
    forbidden_values: Sequence[str],
) -> ScanResult:
    """Validate exact schema and scan serialized safe JSON without returning findings."""
    try:
        validated = _validated_json_value(report)
        _validate_report(validated)
        serialized = json.dumps(
            validated,
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, RecursionError, DiscoverySanitizationError):
        raise DiscoverySanitizationError from None

    lowered = serialized.casefold()
    if (
        any(marker in lowered for marker in _FORBIDDEN_MARKERS)
        or _JWT.search(serialized) is not None
        or _PHONE.search(serialized) is not None
        or _ABSOLUTE_PRIVATE_PATH.search(serialized) is not None
        or any(value and value in serialized for value in forbidden_values)
    ):
        raise DiscoverySanitizationError
    return ScanResult(passed=True, finding_count=0)


def _validated_json_value(value: object) -> JsonValue:
    if value is None or type(value) in (str, bool, int):
        return cast(JsonValue, value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise DiscoverySanitizationError
        return value
    if isinstance(value, list):
        return [_validated_json_value(item) for item in value]
    if isinstance(value, Mapping):
        result: JsonObject = {}
        for key, nested in value.items():
            if not isinstance(key, str):
                raise DiscoverySanitizationError
            result[key] = _validated_json_value(nested)
        return result
    raise DiscoverySanitizationError


def _validate_report(value: JsonValue) -> None:
    if not isinstance(value, dict) or set(value) != _TOP_LEVEL_KEYS:
        raise DiscoverySanitizationError
    if value["schema_version"] != 2:
        raise DiscoverySanitizationError
    version = value["integration_version"]
    session_hour = value["session_started_utc_hour"]
    if (
        not isinstance(version, str)
        or _INTEGRATION_VERSION.fullmatch(version) is None
        or not isinstance(session_hour, str)
        or _SESSION_HOUR.fullmatch(session_hour) is None
        or type(value["wss_baseline_succeeded"]) is not bool
    ):
        raise DiscoverySanitizationError
    _validate_archive(value["raw_archive"])
    cycles = _validate_cycles(value["cycles"])
    _validate_coverage(value["coverage"], cycles)
    _validate_candidates(value["candidates"], cycles)
    _validate_limits(value["limits"])
    _validate_statistics(value["statistics"], cycles)
    if value["sanitization_scan"] != {"passed": True, "finding_count": 0}:
        raise DiscoverySanitizationError


def _validate_limits(value: JsonValue) -> None:
    if not isinstance(value, dict) or set(value) != _LIMIT_KEYS:
        raise DiscoverySanitizationError
    if any(value[key] != expected for key, expected in _INVARIANT_LIMITS.items()):
        raise DiscoverySanitizationError
    timeout_profile = (
        value["stage_timeout_seconds"],
        value["session_timeout_seconds"],
    )
    if timeout_profile not in _TIMEOUT_PROFILES:
        raise DiscoverySanitizationError


def _validate_archive(value: JsonValue) -> None:
    if value == {"enabled": False, "status": "not_requested"}:
        return
    if not isinstance(value, dict) or set(value) != {
        "enabled",
        "status",
        "session_id",
        "event_count",
        "file_bytes",
        "sha256",
    }:
        raise DiscoverySanitizationError
    if (
        value["enabled"] is not True
        or value["status"] != "complete"
        or not isinstance(value["session_id"], str)
        or _SESSION_ID.fullmatch(value["session_id"]) is None
        or not _is_non_negative_int(value["event_count"])
        or not _is_non_negative_int(value["file_bytes"])
        or not isinstance(value["sha256"], str)
        or _SHA256.fullmatch(value["sha256"]) is None
    ):
        raise DiscoverySanitizationError


def _validate_cycles(value: JsonValue) -> list[JsonObject]:
    if not isinstance(value, list) or len(value) > 128:
        raise DiscoverySanitizationError
    cycles: list[JsonObject] = []
    seen: set[str] = set()
    previous_order: tuple[int, str] | None = None
    for item in value:
        if not isinstance(item, dict):
            raise DiscoverySanitizationError
        base = {"cycle_id", "experiment", "round", "completed", "invalid", "phases"}
        optional = {
            "source_level",
            "target_level",
            "source_temperature",
            "target_temperature",
        }
        if not base <= set(item) or set(item) - base - optional:
            raise DiscoverySanitizationError
        experiment = item["experiment"]
        if not isinstance(experiment, str):
            raise DiscoverySanitizationError
        request = build_step_request(
            experiment=experiment,
            round_number=item["round"],
            source_level=item.get("source_level"),
            target_level=item.get("target_level"),
            source_temperature=item.get("source_temperature"),
            target_temperature=item.get("target_temperature"),
        )
        cycle_id = item["cycle_id"]
        if (
            cycle_id != request.cycle_id
            or cycle_id in seen
            or type(item["completed"]) is not bool
            or type(item["invalid"]) is not bool
        ):
            raise DiscoverySanitizationError
        expected_keys = set(base)
        for key in optional:
            if getattr(request, key) is not None:
                expected_keys.add(key)
        if set(item) != expected_keys:
            raise DiscoverySanitizationError
        _validate_phases(item["phases"], request.experiment)
        order = (tuple(EXPERIMENT_CATALOG).index(request.experiment), request.cycle_id)
        if previous_order is not None and order < previous_order:
            raise DiscoverySanitizationError
        previous_order = order
        seen.add(cycle_id)
        cycles.append(item)
    return cycles


def _validate_phases(value: JsonValue, experiment: DiscoveryExperiment) -> None:
    if not isinstance(value, list) or len(value) > 32:
        raise DiscoverySanitizationError
    legal_phases = frozenset(definition_for(experiment).phases)
    for item in value:
        if not isinstance(item, dict) or set(item) != {
            "phase",
            "attempt",
            "snapshot_succeeded",
            "invalid",
            "timed_out",
            "changes",
            "restorations",
        }:
            raise DiscoverySanitizationError
        phase_value = item["phase"]
        if not isinstance(phase_value, str):
            raise DiscoverySanitizationError
        try:
            phase = DiscoveryPhase(phase_value)
        except (TypeError, ValueError):
            raise DiscoverySanitizationError from None
        if (
            phase not in legal_phases
            or not _is_bounded_int(item["attempt"], 1, 64)
            or type(item["snapshot_succeeded"]) is not bool
            or type(item["invalid"]) is not bool
            or type(item["timed_out"]) is not bool
        ):
            raise DiscoverySanitizationError
        _validate_changes(item["changes"])
        _validate_restorations(item["restorations"])


def _validate_changes(value: JsonValue) -> None:
    if not isinstance(value, list) or len(value) > 256:
        raise DiscoverySanitizationError
    previous_path = ""
    for item in value:
        if not isinstance(item, dict):
            raise DiscoverySanitizationError
        base = {"path", "data_type", "direction", "transient_count"}
        path = item.get("path")
        data_type = item.get("data_type")
        if (
            not isinstance(path, str)
            or _PATH.fullmatch(path) is None
            or path < previous_path
            or not isinstance(data_type, str)
            or data_type not in _VALUE_KINDS
            or item.get("direction") not in _DIRECTIONS
            or not _is_bounded_int(item.get("transient_count"), 0, 256)
        ):
            raise DiscoverySanitizationError
        previous_path = path
        if data_type == "timestamp":
            if set(item) not in (base, base | {"delta"}):
                raise DiscoverySanitizationError
            if "delta" in item and not _is_finite_number(item["delta"]):
                raise DiscoverySanitizationError
        else:
            if set(item) != base | {"before", "after"}:
                raise DiscoverySanitizationError
            _validate_public_value(item["before"], allow_none=True)
            _validate_public_value(item["after"], allow_none=True)


def _validate_restorations(value: JsonValue) -> None:
    if not isinstance(value, list) or len(value) > 256:
        raise DiscoverySanitizationError
    previous = ""
    for item in value:
        if (
            not isinstance(item, dict)
            or set(item) != {"path", "restored"}
            or not isinstance(item["path"], str)
            or _PATH.fullmatch(item["path"]) is None
            or item["path"] < previous
            or type(item["restored"]) is not bool
        ):
            raise DiscoverySanitizationError
        previous = item["path"]


def _validate_coverage(value: JsonValue, cycles: Sequence[JsonObject]) -> None:
    if not isinstance(value, list) or len(value) != len(EXPERIMENT_CATALOG):
        raise DiscoverySanitizationError
    for item, experiment in zip(value, EXPERIMENT_CATALOG, strict=True):
        if not isinstance(item, dict) or set(item) != {
            "experiment",
            "status",
            "required_rounds",
            "completed_rounds",
        }:
            raise DiscoverySanitizationError
        relevant = [cycle for cycle in cycles if cycle["experiment"] == experiment.value]
        usable = [cycle for cycle in relevant if _public_cycle_usable(cycle)]
        required = 8 if experiment is DiscoveryExperiment.GLOBAL_FAN_LEVEL else 2
        completed = len({cast(str, cycle["cycle_id"]) for cycle in usable})
        expected_status = DiscoveryCoverage.NOT_STARTED.value
        if relevant:
            expected_status = DiscoveryCoverage.PARTIAL.value
        if _public_coverage_complete(experiment, usable):
            expected_status = DiscoveryCoverage.COMPLETE.value
        if item != {
            "experiment": experiment.value,
            "status": expected_status,
            "required_rounds": required,
            "completed_rounds": completed,
        }:
            raise DiscoverySanitizationError


def _public_coverage_complete(
    experiment: DiscoveryExperiment,
    cycles: Sequence[JsonObject],
) -> bool:
    if experiment is DiscoveryExperiment.GLOBAL_FAN_LEVEL:
        sources = {cycle.get("source_level") for cycle in cycles}
        if len(sources) != 1 or None in sources:
            return False
        source = next(iter(sources))
        expected = {
            (target, round_number)
            for target in (1, 2, 3, 4, 5)
            if target != source
            for round_number in (1, 2)
        }
        return {(cycle.get("target_level"), cycle["round"]) for cycle in cycles} == expected
    if experiment is DiscoveryExperiment.AI_TARGET_TEMPERATURE:
        pairs = {
            (cycle.get("source_temperature"), cycle.get("target_temperature")) for cycle in cycles
        }
        return len(pairs) == 1 and {cycle["round"] for cycle in cycles} == {1, 2}
    return {cycle["round"] for cycle in cycles} == {1, 2}


def _validate_candidates(value: JsonValue, cycles: Sequence[JsonObject]) -> None:
    if not isinstance(value, list) or len(value) > 4096:
        raise DiscoverySanitizationError
    cycle_experiments = {cycle["cycle_id"]: cycle["experiment"] for cycle in cycles}
    previous_order: tuple[int, int, str, tuple[str, ...]] | None = None
    validated_candidates: list[JsonObject] = []
    for item in value:
        if not isinstance(item, dict):
            raise DiscoverySanitizationError
        base = {
            "experiment",
            "role",
            "path",
            "data_type",
            "classification",
            "association",
            "value_mappings",
            "evidence_cycles",
        }
        role = item.get("role")
        expected_keys = base | ({"carrier"} if role == "carrier" else set())
        if set(item) != expected_keys:
            raise DiscoverySanitizationError
        experiment_value = item["experiment"]
        if not isinstance(experiment_value, str):
            raise DiscoverySanitizationError
        try:
            experiment = DiscoveryExperiment(experiment_value)
        except (TypeError, ValueError):
            raise DiscoverySanitizationError from None
        definition = definition_for(experiment)
        if (
            not isinstance(role, str)
            or role not in _ROLES
            or not _role_matches(role, definition.kind)
        ):
            raise DiscoverySanitizationError
        if role == "carrier" and (
            definition.carrier is None or item["carrier"] != definition.carrier.value
        ):
            raise DiscoverySanitizationError
        classification = item["classification"]
        association = item["association"]
        path = item["path"]
        data_type = item["data_type"]
        evidence = item["evidence_cycles"]
        mappings = item["value_mappings"]
        if (
            classification not in _CLASSIFICATIONS
            or association not in (None, "dedicated", "shared")
            or not isinstance(evidence, list)
            or not all(isinstance(cycle_id, str) for cycle_id in evidence)
            or evidence != sorted(cast(list[str], evidence))
            or len(set(evidence)) != len(evidence)
            or any(cycle_experiments.get(cycle_id) != experiment.value for cycle_id in evidence)
            or not isinstance(mappings, list)
        ):
            raise DiscoverySanitizationError
        if path is None:
            if (
                data_type is not None
                or classification not in {"not_observed", "invalid"}
                or association is not None
                or mappings
            ):
                raise DiscoverySanitizationError
        elif (
            not isinstance(path, str)
            or _PATH.fullmatch(path) is None
            or not isinstance(data_type, str)
            or data_type not in _VALUE_KINDS
        ):
            raise DiscoverySanitizationError
        if classification == "confirmed_candidate" and association not in {
            "dedicated",
            "shared",
        }:
            raise DiscoverySanitizationError
        if classification != "confirmed_candidate" and association is not None:
            raise DiscoverySanitizationError
        _validate_mappings(
            mappings,
            experiment=experiment,
            role=role,
            carrier=definition.carrier,
        )
        order = (
            tuple(EXPERIMENT_CATALOG).index(experiment),
            {"mode": 0, "parameter": 1, "carrier": 2, "idle": 3}[role],
            "" if path is None else path,
            tuple(cast(list[str], evidence)),
        )
        if previous_order is not None and order < previous_order:
            raise DiscoverySanitizationError
        previous_order = order
        validated_candidates.append(item)

    by_path: dict[str, list[JsonObject]] = {}
    for candidate in validated_candidates:
        path = candidate["path"]
        if isinstance(path, str) and candidate["role"] != "idle":
            by_path.setdefault(path, []).append(candidate)
    for path_candidates in by_path.values():
        confirmed = [
            candidate
            for candidate in path_candidates
            if candidate["classification"] == "confirmed_candidate"
        ]
        if not confirmed:
            continue
        if len(path_candidates) == 1:
            if confirmed[0]["association"] != "dedicated":
                raise DiscoverySanitizationError
            continue
        if len(confirmed) != len(path_candidates) or any(
            candidate["association"] != "shared" for candidate in confirmed
        ):
            raise DiscoverySanitizationError
        signatures = {
            json.dumps(candidate["value_mappings"], sort_keys=True, separators=(",", ":"))
            for candidate in confirmed
        }
        if len(signatures) != len(confirmed):
            raise DiscoverySanitizationError


def _validate_mappings(
    value: list[JsonValue],
    *,
    experiment: DiscoveryExperiment,
    role: str,
    carrier: DiscoveryExperiment | None,
) -> None:
    labels: set[str] = set()
    for item in value:
        if (
            not isinstance(item, dict)
            or set(item) != {"label", "value"}
            or not isinstance(item["label"], str)
            or item["label"] in labels
        ):
            raise DiscoverySanitizationError
        labels.add(item["label"])
        _validate_public_value(item["value"], allow_none=False)
    if role == "mode":
        allowed = {"off", "on"}
    elif role == "parameter" and experiment is DiscoveryExperiment.GLOBAL_FAN_LEVEL:
        allowed = {f"level_{level}" for level in range(1, 6)}
    elif role == "parameter":
        allowed = {f"temperature_{temperature}" for temperature in range(30, 43)}
    elif role == "carrier" and carrier is not None:
        allowed = {"off", carrier.value}
    else:
        allowed = set()
    if not labels <= allowed:
        raise DiscoverySanitizationError


def _validate_public_value(value: JsonValue, *, allow_none: bool) -> None:
    if value is None:
        if allow_none:
            return
        raise DiscoverySanitizationError
    if not isinstance(value, dict) or not isinstance(value.get("type"), str):
        raise DiscoverySanitizationError
    value_type = value["type"]
    if value_type == "boolean":
        valid = set(value) == {"type", "value"} and type(value["value"]) is bool
    elif value_type == "number":
        number = value.get("value")
        fingerprint = value.get("fingerprint")
        valid = (
            set(value) == {"type", "value"}
            and isinstance(number, (int, float))
            and not isinstance(number, bool)
            and math.isfinite(number)
            and -1000 <= number <= 1000
        ) or (
            set(value) == {"type", "representation", "fingerprint"}
            and value.get("representation") == "fingerprint"
            and isinstance(fingerprint, str)
            and _HMAC.fullmatch(fingerprint) is not None
        )
    elif value_type == "null":
        valid = set(value) == {"type", "occurrences"} and value["occurrences"] == 1
    elif value_type == "string":
        fingerprint = value.get("fingerprint")
        valid = (
            set(value) == {"type", "length", "fingerprint"}
            and _is_bounded_int(value["length"], 0, 65_536)
            and isinstance(fingerprint, str)
            and _HMAC.fullmatch(fingerprint) is not None
        )
    elif value_type == "timestamp":
        valid = set(value) == {"type", "precision"} and value["precision"] in {
            "seconds",
            "milliseconds",
        }
    elif value_type in {"object", "array"}:
        fingerprint = value.get("fingerprint")
        valid = (
            set(value) == {"type", "depth", "elements", "fingerprint"}
            and _is_bounded_int(value["depth"], 1, 4)
            and _is_bounded_int(value["elements"], 1, 256)
            and isinstance(fingerprint, str)
            and _HMAC.fullmatch(fingerprint) is not None
        )
    else:
        valid = False
    if not valid:
        raise DiscoverySanitizationError


def _validate_statistics(value: JsonValue, cycles: Sequence[JsonObject]) -> None:
    if not isinstance(value, dict) or set(value) != {
        "completed_cycles",
        "invalid_cycles",
        "timeouts",
        "restore_failures",
    }:
        raise DiscoverySanitizationError
    expected = {
        "completed_cycles": sum(_public_cycle_usable(cycle) for cycle in cycles),
        "invalid_cycles": sum(_public_cycle_invalid(cycle) for cycle in cycles),
        "timeouts": sum(_public_cycle_timed_out(cycle) for cycle in cycles),
        "restore_failures": sum(
            restoration["restored"] is False
            for cycle in cycles
            for phase in cast(list[JsonObject], cycle["phases"])
            for restoration in cast(list[JsonObject], phase["restorations"])
        ),
    }
    if value != expected:
        raise DiscoverySanitizationError


def _public_cycle_invalid(cycle: JsonObject) -> bool:
    return cycle["invalid"] is True or any(
        phase["invalid"] is True for phase in cast(list[JsonObject], cycle["phases"])
    )


def _public_cycle_timed_out(cycle: JsonObject) -> bool:
    return any(phase["timed_out"] is True for phase in cast(list[JsonObject], cycle["phases"]))


def _public_cycle_usable(cycle: JsonObject) -> bool:
    return (
        cycle["completed"] is True
        and not _public_cycle_invalid(cycle)
        and not _public_cycle_timed_out(cycle)
    )


def _role_matches(role: object, kind: ExperimentKind) -> bool:
    if kind is ExperimentKind.MODE:
        return role == "mode"
    if kind is ExperimentKind.PARAMETER:
        return role in {"parameter", "carrier"}
    return role == "idle"


def _is_non_negative_int(value: JsonValue) -> bool:
    return type(value) is int and value >= 0


def _is_bounded_int(value: object, minimum: int, maximum: int) -> bool:
    return type(value) is int and minimum <= value <= maximum


def _is_finite_number(value: JsonValue) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
