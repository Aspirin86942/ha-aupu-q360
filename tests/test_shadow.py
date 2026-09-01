"""Tests for the target-scoped AWS IoT Shadow parser."""

# ruff: noqa: I001

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from custom_components.aupu_q360.errors import AupuProtocolError
from custom_components.aupu_q360.models import DeviceConfig
from custom_components.aupu_q360.shadow import LightShadowUpdate, parse_shadow_update


DEVICE = DeviceConfig(did="123", tag="synthetic")
GET_ACCEPTED = "$aws/things/123/shadow/get/accepted"
UPDATE_ACCEPTED = "$aws/things/123/shadow/update/accepted"


def test_get_accepted_reported_value_is_confirmed_get_reported() -> None:
    """Catch get responses that do not expose the confirmed reported light state."""
    update = parse_shadow_update(
        DEVICE,
        GET_ACCEPTED,
        b'{"state":{"reported":{"123":{"2":{"properties":{"1":true}}}}}}',
    )

    assert update == LightShadowUpdate(True, True, "get_reported")


def test_update_accepted_reported_wins_over_desired() -> None:
    """Catch desired state incorrectly overriding a confirmed reported state."""
    update = parse_shadow_update(
        DEVICE,
        UPDATE_ACCEPTED,
        b'{"state":{"reported":{"123":{"2":{"properties":{"1":false}}}},'
        b'"desired":{"123":{"2":{"properties":{"1":true}}}}}}',
    )

    assert update == LightShadowUpdate(False, True, "reported")


def test_desired_only_value_is_not_confirmed() -> None:
    """Catch desired-only state being represented as a confirmed device update."""
    update = parse_shadow_update(
        DEVICE,
        UPDATE_ACCEPTED,
        b'{"state":{"desired":{"123":{"2":{"properties":{"1":true}}}}}}',
    )

    assert update == LightShadowUpdate(True, False, "desired")


@pytest.mark.parametrize(
    ("topic", "payload"),
    [
        (
            "$aws/things/999/shadow/update/accepted",
            b'{"state":{"reported":{"999":{"2":{"properties":{"1":true}}}}}}',
        ),
        (
            UPDATE_ACCEPTED,
            b'{"state":{"reported":{"123":{"3":{"properties":{"1":true}}}}}}',
        ),
        (
            UPDATE_ACCEPTED,
            b'{"state":{"reported":{"123":{"2":{"properties":{"2":true}}}}}}',
        ),
        ("$aws/things/123/shadow/update/rejected", b"{}"),
    ],
)
def test_unrelated_shadow_topic_or_path_returns_no_update(topic: str, payload: bytes) -> None:
    """Catch unrelated devices and non-light properties mutating the light state."""
    assert parse_shadow_update(DEVICE, topic, payload) is None


@pytest.mark.parametrize(
    "payload",
    [
        b"not-json",
        b'{"state":[]}',
        b'{"state":{"reported":{"123":{"2":{"properties":{"1":1}}}}}}',
        b'{"state":{"reported":{"123":{"2":{"properties":{"1":"true"}}}}}}',
    ],
)
def test_invalid_target_shadow_payload_raises_fixed_protocol_error(payload: bytes) -> None:
    """Catch malformed target state being accepted or untrusted data leaking in errors."""
    with pytest.raises(AupuProtocolError) as raised:
        parse_shadow_update(DEVICE, UPDATE_ACCEPTED, payload)

    assert str(raised.value) == "Service response is invalid"
    assert repr(payload) not in str(raised.value)


@pytest.mark.parametrize("constant", [b"NaN", b"Infinity", b"-Infinity"])
def test_non_standard_json_constants_raise_fixed_protocol_error(constant: bytes) -> None:
    """Catch JSON parser acceptance of non-standard constants outside target state."""
    payload = (
        b'{"extra":'
        + constant
        + b',"state":{"reported":{"123":{"2":{"properties":{"1":true}}}}}}'
    )

    with pytest.raises(AupuProtocolError) as raised:
        parse_shadow_update(DEVICE, UPDATE_ACCEPTED, payload)

    assert str(raised.value) == "Service response is invalid"
    assert repr(payload) not in str(raised.value)
