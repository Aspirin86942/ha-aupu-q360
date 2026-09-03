"""Config Entry-directed Home Assistant actions for the temporary probe."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol, cast

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError

from .const import DOMAIN
from .models import AupuRuntimeData
from .probe import ProbeError, ProbeResponse

START_PROBE = "start_probe"
SAMPLE_PROBE = "sample_probe"
STOP_PROBE = "stop_probe"

_SERVICE_NAMES = (START_PROBE, SAMPLE_PROBE, STOP_PROBE)
_REGISTRY_KEY = f"{DOMAIN}.probe_services"
_ENTRY_ID = vol.All(str, vol.Length(min=1, max=128))
_ENTRY_SCHEMA = vol.Schema(
    {vol.Required("config_entry_id"): _ENTRY_ID},
    extra=vol.PREVENT_EXTRA,
)


class _Probe(Protocol):
    """Exact temporary probe surface accepted from one loaded runtime."""

    async def async_start(self) -> ProbeResponse:
        """Start and baseline the temporary probe."""

    async def async_sample(self) -> ProbeResponse:
        """Return the next adjacent safe diff."""

    async def async_stop_probe(self) -> ProbeResponse:
        """Clear the probe and return a fixed response."""

    async def async_stop(self) -> None:
        """Clear lifecycle resources without a service response."""


@dataclass(slots=True)
class _ProbeServiceRegistry:
    entry_ids: set[str] = field(default_factory=set)


@callback
def async_register_probe_entry(hass: HomeAssistant, entry_id: str) -> None:
    """Register three domain Actions once and add one loaded entry route."""
    registry = cast(_ProbeServiceRegistry | None, hass.data.get(_REGISTRY_KEY))
    if registry is None:
        registry = _ProbeServiceRegistry()
        _register_domain_services(hass)
        hass.data[_REGISTRY_KEY] = registry
    registry.entry_ids.add(entry_id)


@callback
def async_unregister_probe_entry(hass: HomeAssistant, entry_id: str) -> None:
    """Remove one route and unregister Actions after the final loaded entry."""
    registry = cast(_ProbeServiceRegistry | None, hass.data.get(_REGISTRY_KEY))
    if registry is None:
        return
    registry.entry_ids.discard(entry_id)
    if registry.entry_ids:
        return
    for service_name in _SERVICE_NAMES:
        hass.services.async_remove(DOMAIN, service_name)
    hass.data.pop(_REGISTRY_KEY, None)


def _register_domain_services(hass: HomeAssistant) -> None:
    async def handle_start(call: ServiceCall) -> ServiceResponse:
        return await _call_probe(_resolve_probe(hass, call).async_start)

    async def handle_sample(call: ServiceCall) -> ServiceResponse:
        return await _call_probe(_resolve_probe(hass, call).async_sample)

    async def handle_stop(call: ServiceCall) -> ServiceResponse:
        return await _call_probe(_resolve_probe(hass, call).async_stop_probe)

    handlers = {
        START_PROBE: handle_start,
        SAMPLE_PROBE: handle_sample,
        STOP_PROBE: handle_stop,
    }
    for service_name, handler in handlers.items():
        hass.services.async_register(
            DOMAIN,
            service_name,
            handler,
            _ENTRY_SCHEMA,
            supports_response=SupportsResponse.OPTIONAL,
        )


def _resolve_probe(hass: HomeAssistant, call: ServiceCall) -> _Probe:
    entry_id = cast(str, call.data["config_entry_id"])
    registry = cast(_ProbeServiceRegistry | None, hass.data.get(_REGISTRY_KEY))
    if registry is None or entry_id not in registry.entry_ids:
        raise _service_error("probe_inactive")
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != DOMAIN:
        raise _service_error("probe_inactive")
    try:
        runtime = cast(ConfigEntry[AupuRuntimeData], entry).runtime_data
        probe = runtime.probe
    except (AttributeError, RuntimeError, TypeError):
        raise _service_error("probe_inactive") from None
    required = ("async_start", "async_sample", "async_stop_probe", "async_stop")
    if not all(callable(getattr(probe, name, None)) for name in required):
        raise _service_error("probe_inactive")
    return cast(_Probe, probe)


async def _call_probe(action: Callable[[], Awaitable[ProbeResponse]]) -> ServiceResponse:
    try:
        return cast(ServiceResponse, await action())
    except ProbeError as err:
        raise _service_error(err.error_code) from None


def _service_error(error_code: str) -> ServiceValidationError:
    return ServiceValidationError(
        error_code,
        translation_domain=DOMAIN,
        translation_key=error_code,
    )
