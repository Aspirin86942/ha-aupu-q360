"""Target-scoped, in-memory parsing for AWS IoT Shadow light updates."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, NoReturn

from .errors import AupuProtocolError
from .models import DeviceConfig


@dataclass(frozen=True, slots=True)
class LightShadowUpdate:
    """One observed light state and whether the device has confirmed it."""

    is_on: bool
    confirmed: bool
    source: Literal["reported", "desired", "get_reported"]


@dataclass(frozen=True, slots=True)
class AcceptedShadow:
    """One validated target Shadow message with private parsed state."""

    topic_kind: Literal["get", "update"]
    state: dict[str, Any] = field(repr=False)
    client_token: str | None = field(default=None, repr=False)


type PanelMode = Literal[
    "off",
    "ai_thermostatic_warmth",
    "deodorization_sterilization",
    "ventilation",
    "air_blowing",
    "normal_drying",
    "thermostatic_drying",
    "unknown",
]

PANEL_MODE_OPTIONS: tuple[PanelMode, ...] = (
    "off",
    "ai_thermostatic_warmth",
    "deodorization_sterilization",
    "ventilation",
    "air_blowing",
    "normal_drying",
    "thermostatic_drying",
    "unknown",
)

_MODE_BY_VALUE: dict[int, PanelMode] = {
    0: "off",
    18: "ai_thermostatic_warmth",
    21: "deodorization_sterilization",
    7: "ventilation",
    2: "air_blowing",
    9: "normal_drying",
    4: "thermostatic_drying",
}
_MISSING = object()


@dataclass(frozen=True, slots=True)
class PanelFieldUpdate[T]:
    """One reported field, distinguishing omission from an unusable value."""

    present: bool
    value: T | None


@dataclass(frozen=True, slots=True)
class PanelStateUpdate:
    """Only the four confirmed Q360 panel paths from one reported message."""

    mode: PanelFieldUpdate[PanelMode]
    night_light: PanelFieldUpdate[bool]
    fan_level: PanelFieldUpdate[int]
    ai_target_temperature: PanelFieldUpdate[int]


def parse_accepted_shadow(
    device: DeviceConfig, topic: str, payload: bytes
) -> AcceptedShadow | None:
    """Decode one accepted target Shadow without exposing its content."""
    topic_kind = _target_topic_kind(device, topic)
    if topic_kind is None:
        return None
    document = _decode_shadow_document(payload)
    state = document.get("state")
    if not isinstance(state, dict):
        raise AupuProtocolError
    client_token = document.get("clientToken")
    if client_token is not None and (not isinstance(client_token, str) or len(client_token) > 128):
        raise AupuProtocolError
    return AcceptedShadow(
        topic_kind=topic_kind,
        state=state,
        client_token=client_token,
    )


def parse_light_shadow_update(
    device: DeviceConfig, message: AcceptedShadow
) -> LightShadowUpdate | None:
    """Parse the target light state from one already validated Shadow."""
    reported = _extract_light_value(message.state, "reported", device.did)
    if reported is not None:
        return LightShadowUpdate(
            is_on=reported,
            confirmed=True,
            source="get_reported" if message.topic_kind == "get" else "reported",
        )
    desired = _extract_light_value(message.state, "desired", device.did)
    if desired is None:
        return None
    return LightShadowUpdate(is_on=desired, confirmed=False, source="desired")


def parse_panel_shadow_update(
    device: DeviceConfig,
    message: AcceptedShadow,
) -> PanelStateUpdate | None:
    """Parse only confirmed panel fields from the target reported state."""
    reported = message.state.get("reported")
    if reported is None:
        return None
    if not isinstance(reported, dict):
        raise AupuProtocolError
    device_state = reported.get(device.did)
    if device_state is None:
        return None
    if not isinstance(device_state, dict):
        raise AupuProtocolError

    update = PanelStateUpdate(
        mode=_field_update(
            _panel_property(device_state, "3", "2"),
            _normalize_mode,
        ),
        night_light=_field_update(
            _panel_property(device_state, "6", "4"),
            _normalize_bool,
        ),
        fan_level=_field_update(
            _panel_property(device_state, "6", "5"),
            lambda value: _normalize_bounded_int(value, minimum=1, maximum=5),
        ),
        ai_target_temperature=_field_update(
            _panel_property(device_state, "3", "3"),
            lambda value: _normalize_bounded_int(value, minimum=30, maximum=42),
        ),
    )
    return (
        update
        if any(
            field.present
            for field in (
                update.mode,
                update.night_light,
                update.fan_level,
                update.ai_target_temperature,
            )
        )
        else None
    )


def parse_shadow_update(
    device: DeviceConfig, topic: str, payload: bytes
) -> LightShadowUpdate | None:
    """Parse the target device's accepted Shadow state, if it changes the light."""
    message = parse_accepted_shadow(device, topic, payload)
    if message is None:
        return None
    return parse_light_shadow_update(device, message)


def _decode_shadow_document(payload: bytes) -> dict[str, Any]:
    """Decode a Shadow JSON object behind a fixed protocol error."""
    if not isinstance(payload, bytes):
        raise AupuProtocolError
    try:
        document = json.loads(payload.decode("utf-8"), parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise AupuProtocolError from None
    if not isinstance(document, dict):
        raise AupuProtocolError
    return document


def _panel_property(
    device_state: dict[str, Any],
    service_id: str,
    property_id: str,
) -> object:
    service = device_state.get(service_id, _MISSING)
    if service is _MISSING:
        return _MISSING
    if not isinstance(service, dict):
        raise AupuProtocolError
    properties = service.get("properties", _MISSING)
    if properties is _MISSING:
        return _MISSING
    if not isinstance(properties, dict):
        raise AupuProtocolError
    return properties.get(property_id, _MISSING)


def _field_update[T](
    raw_value: object,
    normalize: Callable[[object], T | None],
) -> PanelFieldUpdate[T]:
    if raw_value is _MISSING:
        return PanelFieldUpdate(present=False, value=None)
    return PanelFieldUpdate(present=True, value=normalize(raw_value))


def _normalize_mode(value: object) -> PanelMode | None:
    if type(value) is not int:
        return None
    return _MODE_BY_VALUE.get(value, "unknown")


def _normalize_bool(value: object) -> bool | None:
    return value if type(value) is bool else None


def _normalize_bounded_int(value: object, *, minimum: int, maximum: int) -> int | None:
    return value if type(value) is int and minimum <= value <= maximum else None


def _target_topic_kind(device: DeviceConfig, topic: str) -> Literal["get", "update"] | None:
    """Return an accepted topic kind only when it belongs to the target device."""
    if not isinstance(topic, str):
        return None
    prefix = f"$aws/things/{device.did}/shadow/"
    if topic == prefix + "get/accepted":
        return "get"
    if topic == prefix + "update/accepted":
        return "update"
    return None


def _reject_json_constant(_: str) -> NoReturn:
    """Reject JSON extensions such as NaN and Infinity at the parser boundary."""
    raise ValueError


def _extract_light_value(state: dict[str, Any], section: str, did: str) -> bool | None:
    """Extract the exact target lighting path or distinguish unrelated from invalid data."""
    if section not in state:
        return None
    section_value = state[section]
    if not isinstance(section_value, dict):
        raise AupuProtocolError
    if did not in section_value:
        return None
    device_state = section_value[did]
    if not isinstance(device_state, dict):
        raise AupuProtocolError
    if "2" not in device_state:
        return None
    service_state = device_state["2"]
    if not isinstance(service_state, dict):
        raise AupuProtocolError
    if "properties" not in service_state:
        return None
    properties = service_state["properties"]
    if not isinstance(properties, dict):
        raise AupuProtocolError
    if "1" not in properties:
        return None
    value = properties["1"]
    if type(value) is not bool:
        raise AupuProtocolError
    return value
