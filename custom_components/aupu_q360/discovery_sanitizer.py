"""Fail-closed in-memory sanitization for Q360 discovery evidence."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from collections.abc import Sequence
from typing import Literal

from .discovery_models import SanitizedValue, ScanResult

_PATH_IDENTIFIER = re.compile(r"[0-9]{1,10}")
_TIMESTAMP_SECONDS_MIN = 946_684_800
_TIMESTAMP_SECONDS_MAX = 4_102_444_800
_TIMESTAMP_MILLISECONDS_MIN = 946_684_800_000
_TIMESTAMP_MILLISECONDS_MAX = 4_102_444_800_000
_MAX_STRUCTURE_DEPTH = 4
_MAX_STRUCTURE_NODES = 256
_MAX_NESTED_KEY_LENGTH = 64
_MAX_PACKET_STRING_LENGTH = 65_536


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
        self._session_key: bytes | None = session_key
        self._device_id = device_id

    def sanitize_reported(self, state: object) -> dict[str, SanitizedValue]:
        """Return only aliased properties for the configured target device."""
        if self._session_key is None:
            raise DiscoverySanitizationError
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

    def close(self) -> None:
        """Clear the session HMAC key and reject all later sanitization."""
        self._session_key = None

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
            if -1000 <= value <= 1000:
                return SanitizedValue(
                    kind="number",
                    comparison=value,
                    public={"type": "number", "value": value},
                )
            canonical_number = json.dumps(
                value,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
            fingerprint = self._fingerprint(canonical_number)
            return SanitizedValue(
                kind="number",
                comparison=("number", fingerprint),
                public={
                    "type": "number",
                    "representation": "fingerprint",
                    "fingerprint": fingerprint,
                },
            )
        if isinstance(value, str):
            if len(value) > _MAX_PACKET_STRING_LENGTH:
                raise DiscoverySanitizationError
            fingerprint = self._fingerprint(value.encode("utf-8"))
            return SanitizedValue(
                kind="string",
                comparison=("string", fingerprint),
                public={
                    "type": "string",
                    "length": len(value),
                    "fingerprint": fingerprint,
                },
            )
        if isinstance(value, (dict, list)):
            kind: Literal["object", "array"] = "object" if isinstance(value, dict) else "array"
            fingerprint, depth, elements = self._canonicalize_and_fingerprint(value)
            return SanitizedValue(
                kind=kind,
                comparison=(kind, fingerprint),
                public={
                    "type": kind,
                    "depth": depth,
                    "elements": elements,
                    "fingerprint": fingerprint,
                },
            )
        raise DiscoverySanitizationError

    def _canonicalize_and_fingerprint(
        self, value: dict[object, object] | list[object]
    ) -> tuple[str, int, int]:
        """Validate bounded JSON content and fingerprint its stable canonical bytes."""
        depth, elements = _validate_structure(value)
        try:
            canonical = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeEncodeError):
            raise DiscoverySanitizationError from None
        return self._fingerprint(canonical), depth, elements

    def _fingerprint(self, value: bytes) -> str:
        """Return one fixed-format HMAC or fail after the session is closed."""
        session_key = self._session_key
        if session_key is None:
            raise DiscoverySanitizationError
        digest = hmac.new(session_key, value, hashlib.sha256).hexdigest()[:16]
        return "h-" + "-".join(digest[index : index + 4] for index in range(0, 16, 4))


def validate_discovery_report(
    report: object,
    *,
    forbidden_values: Sequence[str],
) -> ScanResult:
    """Delegate legacy imports to the exact schema-v2 validator."""
    from .discovery_report_schema import validate_discovery_report as validate_v2

    return validate_v2(report, forbidden_values=forbidden_values)


def _timestamp_precision(value: float) -> str | None:
    if _TIMESTAMP_SECONDS_MIN <= value <= _TIMESTAMP_SECONDS_MAX:
        return "seconds"
    if _TIMESTAMP_MILLISECONDS_MIN <= value <= _TIMESTAMP_MILLISECONDS_MAX:
        return "milliseconds"
    return None


def _validate_structure(value: object) -> tuple[int, int]:
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
        elif type(current) is bool or current is None:
            pass
        elif isinstance(current, (int, float)):
            if not math.isfinite(current):
                raise DiscoverySanitizationError
        elif isinstance(current, str):
            if len(current) > _MAX_PACKET_STRING_LENGTH:
                raise DiscoverySanitizationError
        else:
            raise DiscoverySanitizationError
        return maximum

    depth = visit(value, 1)
    return depth, nodes
