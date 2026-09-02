"""Private atomic Home Assistant storage for the latest discovery report."""

from __future__ import annotations

import logging
from collections.abc import Callable

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .discovery_models import JsonObject

_LOGGER = logging.getLogger(__name__)
_STORAGE_VERSION = 1
_STORAGE_PREFIX_V2 = "aupu_q360.discovery_v2"
_STORAGE_PREFIX_LEGACY = "aupu_q360.discovery"

ReportValidator = Callable[[object], object]


class DiscoveryReportStoreError(Exception):
    """Fixed failure for private report persistence operations."""

    def __init__(self) -> None:
        super().__init__("discovery_report_storage_failed")


class DiscoveryReportStore:
    """Validate each report at both sides of one private atomic HA Store."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        validate_report: ReportValidator,
    ) -> None:
        self._validate_report = validate_report
        self._store: Store[JsonObject] = Store(
            hass,
            _STORAGE_VERSION,
            _storage_key_v2(entry_id),
            private=True,
            atomic_writes=True,
        )

    async def async_load(self) -> JsonObject | None:
        """Return a validated report or safely hide missing/corrupt storage."""
        try:
            report = await self._store.async_load()
            if report is None:
                return None
            self._validate_report(report)
            return report
        except Exception:  # noqa: BLE001 - Store and validator details are private
            _LOGGER.error("AUPU discovery report storage failed")
            return None

    async def async_save(self, report: JsonObject) -> None:
        """Validate fully before asking HA for one atomic replacement."""
        try:
            self._validate_report(report)
            await self._store.async_save(report)
        except Exception:  # noqa: BLE001 - expose only the fixed storage error
            _LOGGER.error("AUPU discovery report storage failed")
            raise DiscoveryReportStoreError from None

    async def async_remove(self) -> None:
        """Remove this entry's private report."""
        try:
            await self._store.async_remove()
        except Exception:  # noqa: BLE001 - expose only the fixed storage error
            _LOGGER.error("AUPU discovery report storage failed")
            raise DiscoveryReportStoreError from None

    @classmethod
    async def async_remove_for_entry(
        cls,
        hass: HomeAssistant,
        entry_id: str,
    ) -> None:
        """Remove the exact v2 and legacy keys without loading either report."""
        stores: tuple[Store[JsonObject], ...] = tuple(
            Store(
                hass,
                _STORAGE_VERSION,
                key,
                private=True,
                atomic_writes=True,
            )
            for key in (_storage_key_v2(entry_id), _storage_key_legacy(entry_id))
        )
        failed = False
        for store in stores:
            try:
                await store.async_remove()
            except Exception:  # noqa: BLE001 - attempt both exact keys with fixed text
                failed = True
                _LOGGER.error("AUPU discovery report storage failed")
        if failed:
            raise DiscoveryReportStoreError


def _storage_key_v2(entry_id: str) -> str:
    return f"{_STORAGE_PREFIX_V2}.{entry_id}"


def _storage_key_legacy(entry_id: str) -> str:
    return f"{_STORAGE_PREFIX_LEGACY}.{entry_id}"
