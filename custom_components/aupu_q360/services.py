"""Config Entry-directed Home Assistant actions for read-only discovery."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import cast

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
from .discovery import StateDiscoverySession
from .discovery_models import DiscoveryCapability, DiscoveryTarget, JsonObject
from .errors import DiscoveryError, DiscoveryInvalidTransitionError
from .models import AupuRuntimeData

START_DISCOVERY = "start_discovery"
BEGIN_DISCOVERY_STEP = "begin_discovery_step"
COMPLETE_DISCOVERY_STEP = "complete_discovery_step"
FINISH_DISCOVERY = "finish_discovery"
CANCEL_DISCOVERY = "cancel_discovery"

_SERVICE_NAMES = (
    START_DISCOVERY,
    BEGIN_DISCOVERY_STEP,
    COMPLETE_DISCOVERY_STEP,
    FINISH_DISCOVERY,
    CANCEL_DISCOVERY,
)
_REGISTRY_KEY = f"{DOMAIN}.discovery_services"
_ENTRY_ID = vol.All(str, vol.Length(min=1, max=128))
_ENTRY_SCHEMA = vol.Schema(
    {vol.Required("config_entry_id"): _ENTRY_ID},
    extra=vol.PREVENT_EXTRA,
)
_BEGIN_SCHEMA = vol.Schema(
    {
        vol.Required("config_entry_id"): _ENTRY_ID,
        vol.Required("capability"): vol.In(
            tuple(capability.value for capability in DiscoveryCapability)
        ),
        vol.Required("target"): vol.In(tuple(target.value for target in DiscoveryTarget)),
        vol.Required("round"): vol.All(vol.Coerce(int), vol.In((1, 2))),
    },
    extra=vol.PREVENT_EXTRA,
)
_CLASSIFICATIONS = (
    "confirmed_candidate",
    "ambiguous",
    "observed_unidentified",
    "not_observed",
    "invalid",
)


@dataclass(slots=True)
class _DiscoveryServiceRegistry:
    entry_ids: set[str] = field(default_factory=set)


@callback
def async_register_discovery_entry(hass: HomeAssistant, entry_id: str) -> None:
    """Register five domain actions once and add one loaded entry route."""
    registry = cast(_DiscoveryServiceRegistry | None, hass.data.get(_REGISTRY_KEY))
    if registry is None:
        registry = _DiscoveryServiceRegistry()
        _register_domain_services(hass)
        hass.data[_REGISTRY_KEY] = registry
    registry.entry_ids.add(entry_id)


@callback
def async_unregister_discovery_entry(hass: HomeAssistant, entry_id: str) -> None:
    """Remove one route and unregister domain actions after the final entry."""
    registry = cast(_DiscoveryServiceRegistry | None, hass.data.get(_REGISTRY_KEY))
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
        session = _resolve_session(hass, call)
        await _call_session(session.async_start)
        return {
            "state": "ready",
            "message_code": "discovery_ready_for_step",
        }

    async def handle_begin(call: ServiceCall) -> ServiceResponse:
        session = _resolve_session(hass, call)
        capability = cast(str, call.data["capability"])
        target = cast(str, call.data["target"])
        round_number = cast(int, call.data["round"])
        if not _valid_capability_target(capability, target):
            raise _service_error(DiscoveryInvalidTransitionError.error_code)
        await _call_session(lambda: session.async_begin_step(capability, target, round_number))
        return {
            "state": "observing",
            "message_code": "discovery_ready_for_panel_action",
            "wait_seconds_min": 15,
            "wait_seconds_max": 30,
        }

    async def handle_complete(call: ServiceCall) -> ServiceResponse:
        session = _resolve_session(hass, call)
        await _call_session(session.async_complete_step)
        return {
            "state": "ready",
            "message_code": "discovery_step_recorded",
        }

    async def handle_finish(call: ServiceCall) -> ServiceResponse:
        session = _resolve_session(hass, call)
        report: JsonObject = await _call_session(session.async_finish)
        return _finish_response(report)

    async def handle_cancel(call: ServiceCall) -> ServiceResponse:
        session = _resolve_session(hass, call)
        await _call_session(session.async_cancel)
        return {
            "state": "idle",
            "message_code": "discovery_cancelled",
        }

    handlers = {
        START_DISCOVERY: (handle_start, _ENTRY_SCHEMA),
        BEGIN_DISCOVERY_STEP: (handle_begin, _BEGIN_SCHEMA),
        COMPLETE_DISCOVERY_STEP: (handle_complete, _ENTRY_SCHEMA),
        FINISH_DISCOVERY: (handle_finish, _ENTRY_SCHEMA),
        CANCEL_DISCOVERY: (handle_cancel, _ENTRY_SCHEMA),
    }
    for service_name, (handler, schema) in handlers.items():
        hass.services.async_register(
            DOMAIN,
            service_name,
            handler,
            schema,
            supports_response=SupportsResponse.OPTIONAL,
        )


def _resolve_session(hass: HomeAssistant, call: ServiceCall) -> StateDiscoverySession:
    entry_id = cast(str, call.data["config_entry_id"])
    registry = cast(_DiscoveryServiceRegistry | None, hass.data.get(_REGISTRY_KEY))
    if registry is None or entry_id not in registry.entry_ids:
        raise _service_error(DiscoveryInvalidTransitionError.error_code)
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != DOMAIN:
        raise _service_error(DiscoveryInvalidTransitionError.error_code)
    try:
        runtime = cast(ConfigEntry[AupuRuntimeData], entry).runtime_data
        session = runtime.discovery_session
    except (AttributeError, RuntimeError, TypeError):
        raise _service_error(DiscoveryInvalidTransitionError.error_code) from None
    if not isinstance(session, StateDiscoverySession):
        # Tests and compatible wrappers may provide the exact duck-typed API.
        required = (
            "async_start",
            "async_begin_step",
            "async_complete_step",
            "async_finish",
            "async_cancel",
        )
        if not all(callable(getattr(session, name, None)) for name in required):
            raise _service_error(DiscoveryInvalidTransitionError.error_code)
    return session


async def _call_session[T](action: Callable[[], Awaitable[T]]) -> T:
    try:
        return await action()
    except DiscoveryError as err:
        raise _service_error(err.error_code) from None


def _finish_response(report: JsonObject) -> ServiceResponse:
    counts = {classification: 0 for classification in _CLASSIFICATIONS}
    candidates = report.get("candidates")
    if isinstance(candidates, list):
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            classification = candidate.get("classification")
            if isinstance(classification, str) and classification in counts:
                counts[classification] += 1
    return {
        "state": "idle",
        "message_code": "discovery_report_saved",
        "report_available": True,
        "confirmed_candidate_count": counts["confirmed_candidate"],
        "ambiguous_count": counts["ambiguous"],
        "observed_unidentified_count": counts["observed_unidentified"],
        "not_observed_count": counts["not_observed"],
        "invalid_count": counts["invalid"],
    }


def _valid_capability_target(capability: str, target: str) -> bool:
    if capability == DiscoveryCapability.FAN_LEVEL:
        return target in {
            DiscoveryTarget.OFF,
            DiscoveryTarget.LEVEL_1,
            DiscoveryTarget.LEVEL_2,
            DiscoveryTarget.LEVEL_3,
        }
    if capability == DiscoveryCapability.IDLE_ENVIRONMENT:
        return target == DiscoveryTarget.OFF
    return target in {DiscoveryTarget.OFF, DiscoveryTarget.ON}


def _service_error(error_code: str) -> ServiceValidationError:
    return ServiceValidationError(
        error_code,
        translation_domain=DOMAIN,
        translation_key=error_code,
    )
