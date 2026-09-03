"""Shared lifecycle for read-only AUPU panel-state entities."""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .coordinator import AupuCoordinator


class AupuPanelEntity(Entity):
    """Attach one read-only entity to the shared coordinator and device."""

    _attr_has_entity_name = True
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
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            manufacturer="AUPU",
            model="Q360T5-Pro",
            name="AUPU Q360T5-Pro",
        )

    async def async_added_to_hass(self) -> None:
        self._remove_listener = self._coordinator.async_add_listener(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        if self._remove_listener is not None:
            self._remove_listener()
            self._remove_listener = None
