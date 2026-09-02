"""Fail-closed in-memory sanitization for Q360 discovery evidence."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from collections.abc import Mapping, Sequence
from typing import Literal, cast

from .discovery_models import JsonObject, JsonValue, SanitizedValue, ScanResult

_PATH_IDENTIFIER = re.compile(r"[0-9]{1,10}")
_JWT = re.compile(r"[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
_PHONE = re.compile(r"1[3-9][0-9]{9}")
_SESSION_HOUR = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:00Z")
_INTEGRATION_VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,31}")
_EVIDENCE_STEP = re.compile(
    r"(heating|ventilation|drying|swing|fan_level|timer|idle_environment):"
    r"(off|on|level_1|level_2|level_3):[12]"
)
_TIMESTAMP_SECONDS_MIN = 946_684_800
_TIMESTAMP_SECONDS_MAX = 4_102_444_800
_TIMESTAMP_MILLISECONDS_MIN = 946_684_800_000
_TIMESTAMP_MILLISECONDS_MAX = 4_102_444_800_000
_MAX_STRUCTURE_DEPTH = 4
_MAX_STRUCTURE_NODES = 256
_MAX_NESTED_KEY_LENGTH = 64
_FORBIDDEN_MARKERS = (
    "$aws/things/",
    "bearer ",
    "clienttoken",
    "payload",
    "topic",
    "desired",
    "signer",
)
_CAPABILITIES = frozenset(
    {
        "heating",
        "ventilation",
        "drying",
        "swing",
        "fan_level",
        "timer",
        "idle_environment",
    }
)
_TARGETS = frozenset({"off", "on", "level_1", "level_2", "level_3"})
_VALUE_KINDS = frozenset({"boolean", "number", "null", "string", "timestamp", "object", "array"})
_DIRECTIONS = frozenset(
    {
        "added",
        "removed",
        "increase",
        "decrease",
        "off_to_on",
        "on_to_off",
        "changed",
    }
)
_CLASSIFICATIONS = frozenset(
    {
        "confirmed_candidate",
        "ambiguous",
        "observed_unidentified",
        "not_observed",
        "invalid",
    }
)
_REPORT_KEYS = frozenset(
    {
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
)


class DiscoverySanitizationError(Exception):
    """Fixed error for any unsafe discovery value or report."""

    def __init__(self) -> None:
        super().__init__("discovery_invalid_payload")


class DiscoverySanitizer:
    """Sanitize one discovery session using an unlinkable HMAC key."""

    def __init__(self, *, session_key: bytes, device_id: str) -> None:
        if not isinstance(session_key, bytes) or len(session_key) != 32:
            raise DiscoverySanitizationError
        if not isinstance(device_id, str) or not device_id:
            raise DiscoverySanitizationError
        self._session_key = session_key
        self._device_id = device_id

    def sanitize_reported(self, state: object) -> dict[str, SanitizedValue]:
        """Return only aliased properties for the configured target device."""
        if not isinstance(state, dict):
            raise DiscoverySanitizationError
        if "reported" not in state:
            return {}
        reported = state["reported"]
        if not isinstance(reported, dict):
            raise DiscoverySanitizationError
        if self._device_id not in reported:
            return {}
        device_state = reported[self._device_id]
        if not isinstance(device_state, dict):
            raise DiscoverySanitizationError

        result: dict[str, SanitizedValue] = {}
        for service_id, service in device_state.items():
            self._validate_path_identifier(service_id)
            if not isinstance(service, dict):
                raise DiscoverySanitizationError
            if "properties" not in service:
                continue
            properties = service["properties"]
            if not isinstance(properties, dict):
                raise DiscoverySanitizationError
            for property_id, value in properties.items():
                self._validate_path_identifier(property_id)
                path = f"service/{service_id}/property/{property_id}"
                result[path] = self._sanitize_value(value)
        return result

    def has_target_reported(self, state: object) -> bool:
        """Return whether a full get contains the configured reported device root."""
        if not isinstance(state, dict):
            return False
        reported = state.get("reported")
        return (
            isinstance(reported, dict)
            and self._device_id in reported
            and isinstance(reported[self._device_id], dict)
        )

    @staticmethod
    def _validate_path_identifier(value: object) -> None:
        if not isinstance(value, str) or _PATH_IDENTIFIER.fullmatch(value) is None:
            raise DiscoverySanitizationError

    def _sanitize_value(self, value: object) -> SanitizedValue:
        if type(value) is bool:
            return SanitizedValue(
                kind="boolean",
                comparison=value,
                public={"type": "boolean", "value": value},
            )
        if value is None:
            return SanitizedValue(
                kind="null",
                comparison=None,
                public={"type": "null", "occurrences": 1},
            )
        if isinstance(value, (int, float)):
            if not math.isfinite(value):
                raise DiscoverySanitizationError
            precision = _timestamp_precision(value)
            if precision is not None:
                return SanitizedValue(
                    kind="timestamp",
                    comparison=value,
                    public={"type": "timestamp", "precision": precision},
                )
            return SanitizedValue(
                kind="number",
                comparison=value,
                public={"type": "number", "value": value},
            )
        if isinstance(value, str):
            digest = hmac.new(
                self._session_key,
                value.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()[:16]
            fingerprint = "h-" + "-".join(digest[index : index + 4] for index in range(0, 16, 4))
            return SanitizedValue(
                kind="string",
                comparison=fingerprint,
                public={
                    "type": "string",
                    "length": len(value),
                    "fingerprint": fingerprint,
                },
            )
        if isinstance(value, (dict, list)):
            kind: Literal["object", "array"] = "object" if isinstance(value, dict) else "array"
            depth, elements = _structure_shape(value)
            return SanitizedValue(
                kind=kind,
                comparison=(kind, depth, elements),
                public={"type": kind, "depth": depth, "elements": elements},
            )
        raise DiscoverySanitizationError


def validate_discovery_report(
    report: object,
    *,
    forbidden_values: Sequence[str],
) -> ScanResult:
    """Reject non-JSON or sensitive report content without returning findings."""
    try:
        validated = _validated_json_value(report)
        _validate_report_schema(validated)
        serialized = json.dumps(
            validated,
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, RecursionError):
        raise DiscoverySanitizationError from None

    lowered = serialized.casefold()
    if any(marker in lowered for marker in _FORBIDDEN_MARKERS):
        raise DiscoverySanitizationError
    if _JWT.search(serialized) is not None or _PHONE.search(serialized) is not None:
        raise DiscoverySanitizationError
    if any(value and value in serialized for value in forbidden_values):
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


def _validate_report_schema(value: JsonValue) -> None:
    if not isinstance(value, dict) or set(value) != _REPORT_KEYS:
        raise DiscoverySanitizationError
    if value["schema_version"] != 1:
        raise DiscoverySanitizationError
    integration_version = value["integration_version"]
    session_hour = value["session_started_utc_hour"]
    if (
        not isinstance(integration_version, str)
        or _INTEGRATION_VERSION.fullmatch(integration_version) is None
        or not isinstance(session_hour, str)
        or _SESSION_HOUR.fullmatch(session_hour) is None
        or type(value["wss_baseline_succeeded"]) is not bool
    ):
        raise DiscoverySanitizationError

    steps = value["steps"]
    candidates = value["candidates"]
    if not isinstance(steps, list) or len(steps) > 128:
        raise DiscoverySanitizationError
    if not isinstance(candidates, list) or len(candidates) > 2048:
        raise DiscoverySanitizationError
    for step in steps:
        _validate_report_step(step)
    for candidate in candidates:
        _validate_report_candidate(candidate)

    if value["limits"] != {
        "snapshot_timeout_seconds": 10,
        "step_timeout_seconds": 120,
        "session_timeout_seconds": 1200,
        "max_changes_per_step": 256,
        "mqtt_packet_bytes": 65_536,
    }:
        raise DiscoverySanitizationError
    statistics = value["statistics"]
    if not isinstance(statistics, dict) or set(statistics) != {
        "completed_steps",
        "invalid_steps",
        "timeouts",
    }:
        raise DiscoverySanitizationError
    if not all(_is_non_negative_int(statistics[key]) for key in statistics):
        raise DiscoverySanitizationError
    expected_statistics = {
        "completed_steps": sum(
            isinstance(step, dict)
            and step.get("invalid") is False
            and step.get("timed_out") is False
            for step in steps
        ),
        "invalid_steps": sum(
            isinstance(step, dict) and step.get("invalid") is True for step in steps
        ),
        "timeouts": sum(isinstance(step, dict) and step.get("timed_out") is True for step in steps),
    }
    if statistics != expected_statistics:
        raise DiscoverySanitizationError
    if value["sanitization_scan"] != {"passed": True, "finding_count": 0}:
        raise DiscoverySanitizationError


def _validate_report_step(value: JsonValue) -> None:
    if not isinstance(value, dict) or set(value) != {
        "capability",
        "target",
        "round",
        "snapshot_succeeded",
        "baseline_restored",
        "invalid",
        "timed_out",
        "changes",
    }:
        raise DiscoverySanitizationError
    capability = value["capability"]
    target = value["target"]
    if (
        not isinstance(capability, str)
        or capability not in _CAPABILITIES
        or not isinstance(target, str)
        or target not in _TARGETS
        or not _valid_capability_target(capability, target)
        or type(value["round"]) is not int
        or value["round"] not in (1, 2)
        or type(value["snapshot_succeeded"]) is not bool
        or (value["baseline_restored"] is not None and type(value["baseline_restored"]) is not bool)
        or type(value["invalid"]) is not bool
        or type(value["timed_out"]) is not bool
    ):
        raise DiscoverySanitizationError
    changes = value["changes"]
    if not isinstance(changes, list) or len(changes) > _MAX_STRUCTURE_NODES:
        raise DiscoverySanitizationError
    for change in changes:
        _validate_report_change(change)


def _validate_report_change(value: JsonValue) -> None:
    if not isinstance(value, dict):
        raise DiscoverySanitizationError
    base_keys = {"path", "data_type", "direction", "transient_count"}
    path = value.get("path")
    data_type = value.get("data_type")
    if (
        not isinstance(path, str)
        or re.fullmatch(r"service/[0-9]{1,10}/property/[0-9]{1,10}", path) is None
        or not isinstance(data_type, str)
        or data_type not in _VALUE_KINDS
        or value.get("direction") not in _DIRECTIONS
        or not _is_bounded_int(value.get("transient_count"), 0, 256)
    ):
        raise DiscoverySanitizationError
    if data_type == "timestamp":
        if set(value) not in (base_keys, base_keys | {"delta"}):
            raise DiscoverySanitizationError
        if "delta" in value and not _is_finite_number(value["delta"]):
            raise DiscoverySanitizationError
        return
    if set(value) != base_keys | {"before", "after"}:
        raise DiscoverySanitizationError
    _validate_public_value_or_none(value["before"])
    _validate_public_value_or_none(value["after"])


def _validate_public_value_or_none(value: JsonValue) -> None:
    if value is None:
        return
    if not isinstance(value, dict) or not isinstance(value.get("type"), str):
        raise DiscoverySanitizationError
    value_type = value["type"]
    if value_type == "boolean":
        valid = set(value) == {"type", "value"} and type(value["value"]) is bool
    elif value_type == "number":
        valid = set(value) == {"type", "value"} and _is_finite_number(value["value"])
    elif value_type == "null":
        valid = set(value) == {"type", "occurrences"} and _is_bounded_int(
            value["occurrences"], 1, 256
        )
    elif value_type == "string":
        fingerprint = value.get("fingerprint")
        valid = (
            set(value) == {"type", "length", "fingerprint"}
            and _is_bounded_int(value["length"], 0, 65_536)
            and isinstance(fingerprint, str)
            and re.fullmatch(r"h-(?:[0-9a-f]{4}-){3}[0-9a-f]{4}", fingerprint) is not None
        )
    elif value_type == "timestamp":
        valid = set(value) == {"type", "precision"} and value["precision"] in (
            "seconds",
            "milliseconds",
        )
    elif value_type in ("object", "array"):
        valid = (
            set(value) == {"type", "depth", "elements"}
            and _is_bounded_int(value["depth"], 1, 4)
            and _is_bounded_int(value["elements"], 1, 256)
        )
    else:
        valid = False
    if not valid:
        raise DiscoverySanitizationError


def _validate_report_candidate(value: JsonValue) -> None:
    if not isinstance(value, dict) or set(value) != {
        "capability",
        "path",
        "data_type",
        "classification",
        "evidence_steps",
    }:
        raise DiscoverySanitizationError
    capability = value["capability"]
    path = value["path"]
    data_type = value["data_type"]
    evidence_steps = value["evidence_steps"]
    if (
        not isinstance(capability, str)
        or capability not in _CAPABILITIES
        or value["classification"] not in _CLASSIFICATIONS
        or not isinstance(evidence_steps, list)
        or len(evidence_steps) > 64
        or not all(
            isinstance(step, str) and _EVIDENCE_STEP.fullmatch(step) is not None
            for step in evidence_steps
        )
    ):
        raise DiscoverySanitizationError
    if path is None or data_type is None:
        if path is not None or data_type is not None:
            raise DiscoverySanitizationError
        if value["classification"] not in ("not_observed", "invalid"):
            raise DiscoverySanitizationError
        return
    if (
        not isinstance(path, str)
        or re.fullmatch(r"service/[0-9]{1,10}/property/[0-9]{1,10}", path) is None
        or not isinstance(data_type, str)
        or data_type not in _VALUE_KINDS
    ):
        raise DiscoverySanitizationError


def _valid_capability_target(capability: str, target: str) -> bool:
    if capability == "fan_level":
        return target in {"off", "level_1", "level_2", "level_3"}
    if capability == "idle_environment":
        return target == "off"
    return target in {"off", "on"}


def _is_non_negative_int(value: JsonValue) -> bool:
    return type(value) is int and value >= 0


def _is_bounded_int(value: JsonValue | object, minimum: int, maximum: int) -> bool:
    return type(value) is int and minimum <= value <= maximum


def _is_finite_number(value: JsonValue) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _timestamp_precision(value: float) -> str | None:
    if _TIMESTAMP_SECONDS_MIN <= value <= _TIMESTAMP_SECONDS_MAX:
        return "seconds"
    if _TIMESTAMP_MILLISECONDS_MIN <= value <= _TIMESTAMP_MILLISECONDS_MAX:
        return "milliseconds"
    return None


def _structure_shape(value: object) -> tuple[int, int]:
    nodes = 0

    def visit(current: object, depth: int) -> int:
        nonlocal nodes
        nodes += 1
        if depth > _MAX_STRUCTURE_DEPTH or nodes > _MAX_STRUCTURE_NODES:
            raise DiscoverySanitizationError
        maximum = depth
        if isinstance(current, dict):
            for key, nested in current.items():
                if not isinstance(key, str) or len(key) > _MAX_NESTED_KEY_LENGTH:
                    raise DiscoverySanitizationError
                maximum = max(maximum, visit(nested, depth + 1))
        elif isinstance(current, list):
            for nested in current:
                maximum = max(maximum, visit(nested, depth + 1))
        return maximum

    depth = visit(value, 1)
    return depth, nodes
