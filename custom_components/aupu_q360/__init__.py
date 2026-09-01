"""AUPU Q360 Home Assistant integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import AupuApiClient
from .models import AupuConfigEntryData, AupuRuntimeData
from .signer import AppAuthorizationSigner

_PLATFORMS = (Platform.LIGHT,)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry[AupuRuntimeData]
) -> bool:
    """Build non-serializable runtime objects and forward the light platform."""
    config = AupuConfigEntryData.from_mapping(entry.data)
    signer = AppAuthorizationSigner(config.secrets)
    credential = config.credential
    device = config.device
    entry.runtime_data = AupuRuntimeData(
        signer=signer,
        credential=credential,
        device=device,
        api=AupuApiClient(
            session=async_get_clientsession(hass),
            signer=signer,
            credential=credential,
            device=device,
        ),
    )
    try:
        await hass.config_entries.async_forward_entry_setups(entry, _PLATFORMS)
    except BaseException:
        del entry.runtime_data
        raise
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: ConfigEntry[AupuRuntimeData]
) -> bool:
    """Unload entities, then stop and release runtime-owned background work."""
    if not await hass.config_entries.async_unload_platforms(entry, _PLATFORMS):
        return False
    if hasattr(entry, "runtime_data"):
        seen: set[int] = set()
        for stopper in tuple(entry.runtime_data.stoppers):
            identity = id(stopper)
            if identity in seen:
                continue
            seen.add(identity)
            await stopper.async_stop()
        del entry.runtime_data
    return True
