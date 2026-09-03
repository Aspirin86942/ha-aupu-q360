"""Read-only binary sensors for the optional AUPU Q360 WSS transport."""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.components.binary_sensor import DOMAIN as BINARY_SENSOR_DOMAIN
from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .coordinator import AupuCoordinator
from .entity import AupuPanelEntity
from .models import AupuRuntimeData

_SUFFIXES = ("state_channel", "night_light")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry[AupuRuntimeData],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add connectivity and night-light sensors only when the entry uses WSS."""
    base = entry.unique_id or entry.entry_id
    if not entry.runtime_data.use_wss:
        registry = er.async_get(hass)
        for suffix in _SUFFIXES:
            entity_id = registry.async_get_entity_id(
                BINARY_SENSOR_DOMAIN, DOMAIN, f"{base}_{suffix}"
            )
            if entity_id is not None:
                registry.async_remove(entity_id)
                hass.states.async_remove(entity_id)
        return
    async_add_entities(
        [
            AupuStateChannelBinarySensor(
                coordinator=entry.runtime_data.coordinator,
                entry_id=entry.entry_id,
                unique_id=f"{base}_state_channel",
            ),
            AupuNightLightBinarySensor(
                coordinator=entry.runtime_data.coordinator,
                entry_id=entry.entry_id,
                unique_id=f"{base}_night_light",
            ),
        ]
    )


class AupuNightLightBinarySensor(AupuPanelEntity, BinarySensorEntity):
    """Expose the reported night-light flag without a control surface."""

    _attr_translation_key = "night_light"

    @property
    def is_on(self) -> bool | None:
        return self._coordinator.night_light_is_on

    @property
    def available(self) -> bool:
        return self._coordinator.night_light_available


class AupuStateChannelBinarySensor(BinarySensorEntity):
    """Expose whether the continuous WSS state channel is connected."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "state_channel"

    def __init__(
        self,
        *,
        coordinator: AupuCoordinator,
        entry_id: str,
        unique_id: str,
    ) -> None:
        """Initialize the sensor with the Q360 device identity."""
        self._coordinator = coordinator
        self._remove_listener: Callable[[], None] | None = None
        self._attr_unique_id = unique_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            manufacturer="AUPU",
            model="Q360T5-Pro",
            name="AUPU Q360T5-Pro",
        )

    @property
    def is_on(self) -> bool:
        """Return whether the WSS state channel is currently connected."""
        return self._coordinator.wss_connected

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Expose non-secret health and confidence evidence for the channel."""
        confirmed_at = self._coordinator.last_confirmed_at
        return {
            "healthy": self._coordinator.wss_healthy,
            "state_stale": self._coordinator.state_stale,
            "last_confirmed_at": confirmed_at.isoformat() if confirmed_at is not None else None,
        }

    async def async_added_to_hass(self) -> None:
        """Listen for local coordinator changes while the entity is loaded."""
        self._remove_listener = self._coordinator.async_add_listener(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        """Release the listener exactly once during platform unload."""
        if self._remove_listener is not None:
            self._remove_listener()
            self._remove_listener = None
