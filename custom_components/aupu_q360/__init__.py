"""AUPU Q360 Home Assistant integration."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import AupuApiClient
from .coordinator import AupuCoordinator
from .models import AupuConfigEntryData, AupuRuntimeData
from .probe import PanelStateProbe
from .services import async_register_probe_entry, async_unregister_probe_entry
from .shadow import AcceptedShadow
from .signer import AppAuthorizationSigner

_PLATFORMS = (Platform.LIGHT, Platform.BINARY_SENSOR)
_LOGGER = logging.getLogger(__name__)


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry[AupuRuntimeData]) -> None:
    """Rebuild runtime objects after Config Entry data changes."""
    await hass.config_entries.async_reload(entry.entry_id)


async def _async_teardown_runtime(entry: ConfigEntry[AupuRuntimeData]) -> None:
    """Stop each runtime object once and clear the entry reference."""
    if not hasattr(entry, "runtime_data"):
        return
    teardown_failed = False
    current_task = asyncio.current_task()
    pending_control: BaseException | None = None
    seen: set[int] = set()
    try:
        for stopper in tuple(entry.runtime_data.stoppers):
            identity = id(stopper)
            if identity in seen:
                continue
            seen.add(identity)
            try:
                await stopper.async_stop()
            except asyncio.CancelledError as exc:
                if current_task is not None and current_task.cancelling() > 0:
                    if pending_control is None:
                        pending_control = exc
                else:
                    teardown_failed = True
            except Exception:  # noqa: BLE001 - isolate one stopper failure
                teardown_failed = True
            except BaseException as exc:  # noqa: BLE001 - defer process control
                if pending_control is None:
                    pending_control = exc
    finally:
        del entry.runtime_data
    if teardown_failed:
        _LOGGER.error("AUPU runtime teardown failed")
    if pending_control is not None:
        raise pending_control


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry[AupuRuntimeData]) -> bool:
    """Build non-serializable runtime objects and forward the light platform."""
    config = AupuConfigEntryData.from_mapping(
        entry.data,
        require_user_uuid=False,
        require_unexpired_token=False,
    )
    signer = AppAuthorizationSigner(config.secrets)
    credential = config.credential
    device = config.device
    session = async_get_clientsession(hass)
    entry.runtime_data = AupuRuntimeData(
        signer=signer,
        credential=credential,
        device=device,
        use_wss=config.use_wss,
        user_uuid=config.user_uuid,
        api=AupuApiClient(
            session=session,
            signer=signer,
            credential=credential,
            device=device,
        ),
    )
    coordinator = AupuCoordinator(
        hass=hass,
        entry_id=entry.entry_id,
        credential=credential,
        api=entry.runtime_data.api,
        async_request_reauth=lambda: entry.async_start_reauth(hass),
        session=session,
        device=device,
        use_wss=config.use_wss,
        user_uuid=config.user_uuid,
    )
    entry.runtime_data.coordinator = coordinator
    observer_remover: Callable[[], None] | None = None

    def activate_observer(
        observer: Callable[[AcceptedShadow], None],
        cancel: Callable[[], None],
    ) -> None:
        nonlocal observer_remover
        if observer_remover is not None:
            raise RuntimeError("probe observer already active")
        observer_remover = coordinator.async_set_probe_observer(observer, cancel)

    def deactivate_observer() -> None:
        nonlocal observer_remover
        remove = observer_remover
        observer_remover = None
        if remove is not None:
            remove()

    probe = PanelStateProbe(
        device_id=device.did,
        prepare_transport=coordinator.async_prepare_probe_transport,
        request_shadow_get=coordinator.async_request_shadow_get,
        activate_observer=activate_observer,
        deactivate_observer=deactivate_observer,
        probe_available=lambda: coordinator.probe_available,
    )
    entry.runtime_data.probe = probe
    entry.runtime_data.stoppers.extend((probe, coordinator))
    services_registered = False
    try:
        await coordinator.async_start()
        async_register_probe_entry(hass, entry.entry_id)
        services_registered = True
        await hass.config_entries.async_forward_entry_setups(entry, _PLATFORMS)
    except BaseException:
        if services_registered:
            async_unregister_probe_entry(hass, entry.entry_id)
        await _async_teardown_runtime(entry)
        raise

    @callback
    def cancel_probe_on_stop(_: Event) -> None:
        probe.cancel_from_transport()

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, cancel_probe_on_stop)
    )
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry[AupuRuntimeData]) -> bool:
    """Unload entities, then stop and release runtime-owned background work."""
    if not await hass.config_entries.async_unload_platforms(entry, _PLATFORMS):
        return False
    async_unregister_probe_entry(hass, entry.entry_id)
    await _async_teardown_runtime(entry)
    return True
