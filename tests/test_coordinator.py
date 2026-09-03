"""Formal panel-state coordination tests."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import cast

from homeassistant.core import HomeAssistant

from custom_components.aupu_q360.api import AupuApiClient
from custom_components.aupu_q360.auth import BearerCredential
from custom_components.aupu_q360.coordinator import AupuCoordinator
from custom_components.aupu_q360.models import DeviceConfig
from custom_components.aupu_q360.shadow import AcceptedShadow

DEVICE = DeviceConfig(did="123", tag="synthetic")


def _coordinator() -> AupuCoordinator:
    payload = json.dumps({"exp": int((datetime.now(UTC) + timedelta(days=2)).timestamp())}).encode()
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    credential = BearerCredential.parse(f"e30.{encoded}.signature")
    hass = type("FakeRepairHass", (), {"data": {}})()
    return AupuCoordinator(
        hass=cast(HomeAssistant, hass),
        entry_id="synthetic-entry",
        credential=credential,
        api=cast(AupuApiClient, object()),
        async_request_reauth=lambda: None,
        device=DEVICE,
    )


def _coordinator_with_confirmed_panel_state() -> AupuCoordinator:
    coordinator = _coordinator()
    coordinator.async_apply_wss_connection(connected=True, healthy=False)
    coordinator.async_apply_shadow_message(
        AcceptedShadow(
            topic_kind="get",
            state={
                "reported": {
                    DEVICE.did: {
                        "3": {"properties": {"2": 0, "3": 36}},
                        "6": {"properties": {"4": False, "5": 5}},
                    }
                }
            },
        )
    )
    return coordinator


def test_shadow_message_applies_light_and_panel_before_one_notification() -> None:
    """Catch listeners observing partial state or duplicate message notifications."""
    coordinator = _coordinator()
    observed: list[tuple[bool | None, str | None, int | None]] = []
    coordinator.async_add_listener(
        lambda: observed.append((coordinator.is_on, coordinator.panel_mode, coordinator.fan_level))
    )
    coordinator.async_apply_wss_connection(connected=True, healthy=False)
    observed.clear()

    coordinator.async_apply_shadow_message(
        AcceptedShadow(
            topic_kind="get",
            state={
                "reported": {
                    DEVICE.did: {
                        "2": {"properties": {"1": False}},
                        "3": {"properties": {"2": 7, "3": 36}},
                        "6": {"properties": {"4": False, "5": 5}},
                    }
                }
            },
        )
    )

    assert observed == [(False, "ventilation", 5)]
    assert coordinator.night_light_is_on is False
    assert coordinator.ai_target_temperature == 36
    assert coordinator.panel_mode_available is True
    assert coordinator.night_light_available is True
    assert coordinator.fan_level_available is True
    assert coordinator.ai_target_temperature_available is True


def test_partial_panel_update_preserves_missing_and_clears_only_invalid() -> None:
    """Catch a partial or invalid field clearing unrelated confirmed state."""
    coordinator = _coordinator_with_confirmed_panel_state()

    coordinator.async_apply_shadow_message(
        AcceptedShadow(
            topic_kind="update",
            state={
                "reported": {
                    DEVICE.did: {
                        "3": {"properties": {"3": 29}},
                        "6": {"properties": {"4": True}},
                    }
                }
            },
        )
    )

    assert coordinator.panel_mode == "off"
    assert coordinator.fan_level == 5
    assert coordinator.night_light_is_on is True
    assert coordinator.ai_target_temperature is None
    assert coordinator.panel_mode_available is True
    assert coordinator.ai_target_temperature_available is False


def test_disconnect_retains_values_but_reconnect_requires_current_reported() -> None:
    """Catch stale values becoming available before the new connection confirms them."""
    coordinator = _coordinator_with_confirmed_panel_state()

    coordinator.async_apply_wss_connection(connected=False, healthy=False)
    assert coordinator.panel_mode == "off"
    assert coordinator.fan_level == 5
    assert coordinator.panel_state_available is False

    coordinator.async_apply_wss_connection(connected=True, healthy=False)
    assert coordinator.panel_mode_available is False
    assert coordinator.fan_level_available is False

    coordinator.async_apply_shadow_message(
        AcceptedShadow(
            topic_kind="get",
            state={"reported": {DEVICE.did: {"6": {"properties": {"5": 4}}}}},
        )
    )
    assert coordinator.fan_level == 4
    assert coordinator.fan_level_available is True
    assert coordinator.panel_mode == "off"
    assert coordinator.panel_mode_available is False
