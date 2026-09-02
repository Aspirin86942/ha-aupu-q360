"""Target-scoped, in-memory parsing for AWS IoT Shadow light updates."""

from __future__ import annotations

import json
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
class RawShadowEvent:
    """One byte-exact target Shadow event whose content is always redacted in repr."""

    direction: Literal["incoming", "outgoing"] = field(repr=False)
    topic: str = field(repr=False)
    payload: bytes = field(repr=False)

    def __repr__(self) -> str:
        """Return a fixed representation that cannot expose event content."""
        return "RawShadowEvent(<redacted>)"


@dataclass(frozen=True, slots=True)
class AcceptedShadow:
    """One validated accepted Shadow message with private payload state."""

    topic_kind: Literal["get", "update"]
    state: dict[str, Any] = field(repr=False)
    client_token: str | None = field(default=None, repr=False)
    raw_event: RawShadowEvent = field(repr=False, kw_only=True)


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
        raw_event=RawShadowEvent(direction="incoming", topic=topic, payload=payload),
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
