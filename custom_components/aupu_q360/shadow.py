"""Target-scoped, in-memory parsing for AWS IoT Shadow light updates."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from .errors import AupuProtocolError
from .models import DeviceConfig


@dataclass(frozen=True, slots=True)
class LightShadowUpdate:
    """One observed light state and whether the device has confirmed it."""

    is_on: bool
    confirmed: bool
    source: Literal["reported", "desired", "get_reported"]


def parse_shadow_update(
    device: DeviceConfig, topic: str, payload: bytes
) -> LightShadowUpdate | None:
    """Parse the target device's accepted Shadow state, if it changes the light."""
    topic_kind = _target_topic_kind(device, topic)
    if topic_kind is None:
        return None
    if not isinstance(payload, bytes):
        raise AupuProtocolError
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise AupuProtocolError from None
    if not isinstance(document, dict):
        raise AupuProtocolError
    state = document.get("state")
    if not isinstance(state, dict):
        raise AupuProtocolError

    reported = _extract_light_value(state, "reported", device.did)
    if reported is not None:
        return LightShadowUpdate(
            is_on=reported,
            confirmed=True,
            source="get_reported" if topic_kind == "get" else "reported",
        )
    desired = _extract_light_value(state, "desired", device.did)
    if desired is None:
        return None
    return LightShadowUpdate(is_on=desired, confirmed=False, source="desired")


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
