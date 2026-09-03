"""Read-only formal Q360 panel-state sensors."""

from __future__ import annotations

from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .coordinator import AupuCoordinator
from .entity import AupuPanelEntity
from .models import AupuRuntimeData
from .shadow import PANEL_MODE_OPTIONS, PanelMode

_SUFFIXES = ("current_mode", "fan_level", "ai_target_temperature")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry[AupuRuntimeData],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add formal panel sensors only when the entry uses WSS."""
    base = entry.unique_id or entry.entry_id
    if not entry.runtime_data.use_wss:
        registry = er.async_get(hass)
        for suffix in _SUFFIXES:
            entity_id = registry.async_get_entity_id(SENSOR_DOMAIN, DOMAIN, f"{base}_{suffix}")
            if entity_id is not None:
                registry.async_remove(entity_id)
                hass.states.async_remove(entity_id)
        return
    coordinator = entry.runtime_data.coordinator
    async_add_entities(
        [
            AupuCurrentModeSensor(
                coordinator=coordinator,
                entry_id=entry.entry_id,
                unique_id=f"{base}_current_mode",
            ),
            AupuFanLevelSensor(
                coordinator=coordinator,
                entry_id=entry.entry_id,
                unique_id=f"{base}_fan_level",
            ),
            AupuAiTargetTemperatureSensor(
                coordinator=coordinator,
                entry_id=entry.entry_id,
                unique_id=f"{base}_ai_target_temperature",
            ),
        ]
    )


class AupuCurrentModeSensor(AupuPanelEntity, SensorEntity):
    """Expose the normalized mutually exclusive running mode."""

    _attr_translation_key = "current_mode"
    _attr_device_class = SensorDeviceClass.ENUM

    def __init__(
        self,
        *,
        coordinator: AupuCoordinator,
        entry_id: str,
        unique_id: str,
    ) -> None:
        super().__init__(coordinator=coordinator, entry_id=entry_id, unique_id=unique_id)
        self._attr_options = list(PANEL_MODE_OPTIONS)

    @property
    def native_value(self) -> PanelMode | None:
        return self._coordinator.panel_mode

    @property
    def available(self) -> bool:
        return self._coordinator.panel_mode_available


class AupuFanLevelSensor(AupuPanelEntity, SensorEntity):
    """Expose the normalized read-only fan level."""

    _attr_translation_key = "fan_level"
    _attr_native_unit_of_measurement = "档"

    @property
    def native_value(self) -> int | None:
        return self._coordinator.fan_level

    @property
    def available(self) -> bool:
        return self._coordinator.fan_level_available


class AupuAiTargetTemperatureSensor(AupuPanelEntity, SensorEntity):
    """Expose the normalized read-only AI target temperature."""

    _attr_translation_key = "ai_target_temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    @property
    def native_value(self) -> int | None:
        return self._coordinator.ai_target_temperature

    @property
    def available(self) -> bool:
        return self._coordinator.ai_target_temperature_available
