"""Temporary in-memory reported-only field probe for Q360 development."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from homeassistant.exceptions import HomeAssistantError

type ProbeErrorCode = Literal[
    "probe_busy",
    "probe_inactive",
    "probe_wss_unavailable",
    "probe_snapshot_timeout",
    "probe_invalid_payload",
]
type ProbeValue = bool | int
type PublicProbeValue = ProbeValue | None
type PublicProbeChange = dict[str, str | PublicProbeValue]
type ProbeResponse = dict[str, str | int | list[PublicProbeChange]]

_PATH_ID = re.compile(r"[0-9]{1,10}")
_MIN_INTEGER = -1000
_MAX_INTEGER = 1000
_MAX_PATHS = 256
_MAX_CHANGES = 128


class ProbeError(HomeAssistantError):
    """Expose one fixed probe error code without untrusted context."""

    def __init__(self, error_code: ProbeErrorCode) -> None:
        self.error_code = error_code
        super().__init__(error_code)

    def __repr__(self) -> str:
        return f"ProbeError(error_code={self.error_code!r})"


@dataclass(frozen=True, slots=True)
class ProbeChange:
    """One safe scalar change at a normalized synthetic path."""

    path: str
    before: PublicProbeValue
    after: PublicProbeValue

    def to_public(self) -> PublicProbeChange:
        return {"path": self.path, "before": self.before, "after": self.after}


def extract_probe_snapshot(state: object, device_id: str) -> dict[str, ProbeValue]:
    """Extract only bounded scalars from the target reported device root."""
    if not isinstance(state, dict) or not isinstance(device_id, str) or not device_id:
        raise ProbeError("probe_invalid_payload")
    reported = state.get("reported")
    if not isinstance(reported, dict):
        raise ProbeError("probe_invalid_payload")
    device_state = reported.get(device_id)
    if not isinstance(device_state, dict):
        raise ProbeError("probe_invalid_payload")

    snapshot: dict[str, ProbeValue] = {}
    for service_id, service in device_state.items():
        if not isinstance(service_id, str) or _PATH_ID.fullmatch(service_id) is None:
            raise ProbeError("probe_invalid_payload")
        if not isinstance(service, dict):
            raise ProbeError("probe_invalid_payload")
        if "properties" not in service:
            continue
        properties = service["properties"]
        if not isinstance(properties, dict):
            raise ProbeError("probe_invalid_payload")
        for property_id, value in properties.items():
            if not isinstance(property_id, str) or _PATH_ID.fullmatch(property_id) is None:
                raise ProbeError("probe_invalid_payload")
            retained = _retained_value(value)
            if retained is None:
                continue
            snapshot[f"service/{service_id}/property/{property_id}"] = retained
            if len(snapshot) > _MAX_PATHS:
                raise ProbeError("probe_invalid_payload")
    return snapshot


def diff_probe_snapshots(
    before: Mapping[str, ProbeValue],
    after: Mapping[str, ProbeValue],
) -> tuple[ProbeChange, ...]:
    """Return a bounded, deterministic union diff without bool/int coercion."""
    changes: list[ProbeChange] = []
    for path in sorted(before.keys() | after.keys()):
        before_present = path in before
        after_present = path in after
        before_value = before.get(path)
        after_value = after.get(path)
        if (
            before_present
            and after_present
            and type(before_value) is type(after_value)
            and before_value == after_value
        ):
            continue
        changes.append(
            ProbeChange(
                path=path,
                before=before_value if before_present else None,
                after=after_value if after_present else None,
            )
        )
        if len(changes) > _MAX_CHANGES:
            raise ProbeError("probe_invalid_payload")
    return tuple(changes)


def _retained_value(value: object) -> ProbeValue | None:
    if type(value) is bool:
        return value
    if type(value) is int and _MIN_INTEGER <= value <= _MAX_INTEGER:
        return value
    return None
