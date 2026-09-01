"""The sole AUPU Q360 Home Assistant light entity."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.components.light import LightEntity
from homeassistant.components.light.const import ColorMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .coordinator import AupuCoordinator
from .models import AupuRuntimeData


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry[AupuRuntimeData],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add exactly one Q360 light entity from entry runtime data."""
    del hass
    async_add_entities(
        [
            AupuLight(
                coordinator=entry.runtime_data.coordinator,
                entry_id=entry.entry_id,
                unique_id=entry.unique_id or entry.entry_id,
            )
        ]
    )


class AupuLight(LightEntity):
    """Expose only the confirmed Q360 illumination capability."""

    _attr_has_entity_name = False
    _attr_name = "Q360T5-Pro Light"
    _attr_should_poll = False

    def __init__(
        self,
        *,
        coordinator: AupuCoordinator,
        entry_id: str,
        unique_id: str,
    ) -> None:
        self._coordinator = coordinator
        self._remove_listener: Callable[[], None] | None = None
        self._attr_unique_id = unique_id
        self._attr_supported_color_modes = {ColorMode.ONOFF}
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            manufacturer="AUPU",
            model="Q360T5-Pro",
            name="AUPU Q360T5-Pro",
        )

    @property
    def is_on(self) -> bool | None:
        """Return the coordinator's latest desired or confirmed state."""
        return self._coordinator.is_on

    @property
    def assumed_state(self) -> bool:
        """Return whether the current state is only a desired state."""
        return self._coordinator.assumed_state

    @property
    def extra_state_attributes(self) -> dict[str, bool]:
        """Expose non-secret WSS connection health without affecting HTTPS control."""
        return {
            "wss_connected": self._coordinator.wss_connected,
            "wss_healthy": self._coordinator.wss_healthy,
        }

    async def async_added_to_hass(self) -> None:
        """Listen for coordinator state changes while the entity is loaded."""
        self._remove_listener = self._coordinator.async_add_listener(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        """Release the coordinator listener during platform unload."""
        if self._remove_listener is not None:
            self._remove_listener()
            self._remove_listener = None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the Q360 illumination capability."""
        del kwargs
        await self._coordinator.async_set_light(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the Q360 illumination capability."""
        del kwargs
        await self._coordinator.async_set_light(False)
