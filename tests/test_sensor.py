"""Behavior tests for formal read-only Q360 panel sensors."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Coroutine, Generator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.aupu_q360.api import AupuApiClient
from custom_components.aupu_q360.auth import BearerCredential
from custom_components.aupu_q360.const import DOMAIN
from custom_components.aupu_q360.coordinator import AupuCoordinator
from custom_components.aupu_q360.models import DeviceConfig
from custom_components.aupu_q360.sensor import async_setup_entry as async_setup_sensor
from custom_components.aupu_q360.shadow import PANEL_MODE_OPTIONS, AcceptedShadow

_TEST_LOOP = asyncio.new_event_loop()
DEVICE = DeviceConfig(did="123", tag="synthetic")


@pytest.fixture(scope="module", autouse=True)
def _close_test_loop() -> Generator[None]:
    yield
    _TEST_LOOP.close()


def _run[T](awaitable: Coroutine[Any, Any, T]) -> T:
    return _TEST_LOOP.run_until_complete(awaitable)


def _confirmed_coordinator() -> AupuCoordinator:
    payload = json.dumps({"exp": int((datetime.now(UTC) + timedelta(days=2)).timestamp())}).encode()
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    credential = BearerCredential.parse(f"e30.{encoded}.signature")
    hass = type("FakeRepairHass", (), {"data": {}})()
    coordinator = AupuCoordinator(
        hass=cast(HomeAssistant, hass),
        entry_id="synthetic-entry",
        credential=credential,
        api=cast(AupuApiClient, object()),
        async_request_reauth=lambda: None,
        device=DEVICE,
    )
    coordinator.async_apply_wss_connection(connected=True, healthy=False)
    coordinator.async_apply_shadow_message(
        AcceptedShadow(
            topic_kind="get",
            state={
                "reported": {
                    DEVICE.did: {
                        "3": {"properties": {"2": 7, "3": 36}},
                        "6": {"properties": {"4": False, "5": 5}},
                    }
                }
            },
        )
    )
    return coordinator


def test_wss_setup_adds_three_read_only_panel_sensors() -> None:
    """Catch missing entities, wrong projections, or accidental writable semantics."""
    coordinator = _confirmed_coordinator()
    entry = SimpleNamespace(
        entry_id="synthetic-entry",
        unique_id="synthetic-unique-id",
        runtime_data=SimpleNamespace(use_wss=True, coordinator=coordinator),
    )
    entities: list[object] = []

    _run(async_setup_sensor(cast(HomeAssistant, object()), entry, entities.extend))

    assert [entity.unique_id for entity in entities] == [
        "synthetic-unique-id_current_mode",
        "synthetic-unique-id_fan_level",
        "synthetic-unique-id_ai_target_temperature",
    ]
    mode, fan, temperature = entities
    assert mode.native_value == "ventilation"
    assert mode.options == list(PANEL_MODE_OPTIONS)
    assert fan.native_value == 5
    assert fan.native_unit_of_measurement == "档"
    assert temperature.native_value == 36
    assert temperature.device_class is SensorDeviceClass.TEMPERATURE
    assert temperature.native_unit_of_measurement == UnitOfTemperature.CELSIUS
    assert all(entity.available for entity in entities)


def test_https_only_setup_removes_all_prior_panel_sensor_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch WSS-to-HTTPS migration leaving any panel sensor registered."""
    lookups: list[tuple[str, str, str]] = []
    registry_removed: list[str] = []
    state_removed: list[str] = []

    class FakeRegistry:
        def async_get_entity_id(self, domain: str, platform: str, unique_id: str) -> str:
            lookups.append((domain, platform, unique_id))
            return f"sensor.{unique_id}"

        def async_remove(self, entity_id: str) -> None:
            registry_removed.append(entity_id)

    monkeypatch.setattr(er, "async_get", lambda _: FakeRegistry())
    hass = SimpleNamespace(states=SimpleNamespace(async_remove=state_removed.append))
    entry = SimpleNamespace(
        entry_id="synthetic-entry",
        unique_id="synthetic-unique-id",
        runtime_data=SimpleNamespace(
            use_wss=False,
            coordinator=_confirmed_coordinator(),
        ),
    )
    entities: list[object] = []

    _run(async_setup_sensor(cast(HomeAssistant, hass), entry, entities.extend))

    expected_unique_ids = [
        "synthetic-unique-id_current_mode",
        "synthetic-unique-id_fan_level",
        "synthetic-unique-id_ai_target_temperature",
    ]
    assert entities == []
    assert lookups == [(SENSOR_DOMAIN, DOMAIN, unique_id) for unique_id in expected_unique_ids]
    assert registry_removed == [f"sensor.{unique_id}" for unique_id in expected_unique_ids]
    assert state_removed == registry_removed
