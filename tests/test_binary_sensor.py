"""Behavior tests for the optional Q360 state-channel binary sensor."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Coroutine, Generator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.core import HomeAssistant

from custom_components.aupu_q360.api import AupuApiClient
from custom_components.aupu_q360.auth import BearerCredential
from custom_components.aupu_q360.binary_sensor import AupuStateChannelBinarySensor
from custom_components.aupu_q360.binary_sensor import async_setup_entry as async_setup_binary_sensor
from custom_components.aupu_q360.coordinator import AupuCoordinator
from custom_components.aupu_q360.light import AupuLight

_TEST_LOOP = asyncio.new_event_loop()


@pytest.fixture(scope="module", autouse=True)
def _close_test_loop() -> Generator[None]:
    yield
    _TEST_LOOP.close()


def _run[T](awaitable: Coroutine[Any, Any, T]) -> T:
    return _TEST_LOOP.run_until_complete(awaitable)


class FakeApi:
    """Keep the external HTTPS boundary fake while testing local entities."""

    async def set_light(self, is_on: bool) -> object:
        del is_on
        return object()


def _coordinator() -> AupuCoordinator:
    """Construct the real coordinator without a WSS transport."""
    payload = json.dumps({"exp": int((datetime.now(UTC) + timedelta(days=2)).timestamp())})
    token = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    credential = BearerCredential.parse(f"e30.{token}.signature")
    hass = type("FakeRepairHass", (), {"data": {}})()
    return AupuCoordinator(
        hass=cast(HomeAssistant, hass),
        entry_id="synthetic-entry",
        credential=credential,
        api=cast(AupuApiClient, FakeApi()),
        async_request_reauth=lambda: None,
    )


class RecordingStateChannel(AupuStateChannelBinarySensor):
    """Observe HA write behavior without requiring HA's runtime fixture."""

    def __init__(self, coordinator: AupuCoordinator) -> None:
        super().__init__(
            coordinator=coordinator,
            entry_id="synthetic-entry",
            unique_id="synthetic-unique-id_state_channel",
        )
        self.writes = 0

    def async_write_ha_state(self) -> None:
        self.writes += 1


def test_wss_setup_exposes_connectivity_state_and_https_only_adds_nothing() -> None:
    """Catch exposing a state channel without WSS or leaking device-private identifiers."""
    coordinator = _coordinator()
    wss_entities: list[AupuStateChannelBinarySensor] = []
    https_entities: list[AupuStateChannelBinarySensor] = []

    def add_wss_entities(new_entities: list[AupuStateChannelBinarySensor]) -> None:
        wss_entities.extend(new_entities)

    def add_https_entities(new_entities: list[AupuStateChannelBinarySensor]) -> None:
        https_entities.extend(new_entities)

    wss_entry = SimpleNamespace(
        entry_id="synthetic-entry",
        unique_id="synthetic-unique-id",
        runtime_data=SimpleNamespace(use_wss=True, coordinator=coordinator),
    )
    https_only_entry = SimpleNamespace(
        entry_id="synthetic-https-entry",
        unique_id="synthetic-https-unique-id",
        runtime_data=SimpleNamespace(use_wss=False, coordinator=coordinator),
    )
    light_entity = AupuLight(
        coordinator=coordinator,
        entry_id=wss_entry.entry_id,
        unique_id=wss_entry.unique_id,
    )

    _run(async_setup_binary_sensor(cast(HomeAssistant, object()), wss_entry, add_wss_entities))

    assert len(wss_entities) == 1
    entity = wss_entities[0]
    assert entity.unique_id == "synthetic-unique-id_state_channel"
    assert entity.device_class is BinarySensorDeviceClass.CONNECTIVITY
    assert entity.is_on is False
    assert entity.device_info == light_entity.device_info
    assert "123456789" not in entity.unique_id
    assert "synthetic-tag" not in repr(entity.device_info)

    coordinator.async_apply_wss_connection(connected=True, healthy=False)
    assert entity.is_on is True
    assert entity.extra_state_attributes == {
        "healthy": False,
        "state_stale": True,
        "last_confirmed_at": None,
    }

    coordinator.async_apply_wss_connection(connected=False, healthy=False)
    assert entity.is_on is False
    assert entity.extra_state_attributes["state_stale"] is True

    _run(async_setup_binary_sensor(cast(HomeAssistant, object()), https_only_entry, add_https_entities))

    assert https_entities == []


def test_state_channel_listener_is_removed_once_and_stops_writes() -> None:
    """Catch a removed state-channel entity receiving future coordinator updates."""
    coordinator = _coordinator()
    entity = RecordingStateChannel(coordinator)
    _run(entity.async_added_to_hass())

    coordinator.async_apply_wss_connection(connected=True, healthy=True)
    assert entity.writes == 1

    _run(entity.async_will_remove_from_hass())
    _run(entity.async_will_remove_from_hass())
    coordinator.async_apply_wss_connection(connected=False, healthy=False)

    assert entity.writes == 1
