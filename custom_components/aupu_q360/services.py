"""Config Entry-directed Home Assistant actions for v2 read-only discovery."""

from __future__ import annotations

import re
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
from .discovery_catalog import EXPERIMENT_CATALOG, build_step_request
from .discovery_models import DiscoveryProgress, DiscoveryStepRequest, JsonObject
from .errors import (
    DiscoveryError,
    DiscoveryInvalidParameterError,
    DiscoveryInvalidTransitionError,
)
from .models import AupuRuntimeData

START_DISCOVERY = "start_discovery"
BEGIN_DISCOVERY_STEP = "begin_discovery_step"
ADVANCE_DISCOVERY_STEP = "advance_discovery_step"
FINISH_DISCOVERY = "finish_discovery"
CANCEL_DISCOVERY = "cancel_discovery"

_SERVICE_NAMES = (
    START_DISCOVERY,
    BEGIN_DISCOVERY_STEP,
    ADVANCE_DISCOVERY_STEP,
    FINISH_DISCOVERY,
    CANCEL_DISCOVERY,
)
_REGISTRY_KEY = f"{DOMAIN}.discovery_services"
_ENTRY_ID = vol.All(str, vol.Length(min=1, max=128))
_EXPERIMENT = vol.In(tuple(experiment.value for experiment in EXPERIMENT_CATALOG))
_INTEGER_TEXT = re.compile(r"[0-9]+")
_CLASSIFICATIONS = (
    "confirmed_candidate",
    "ambiguous",
    "observed_unidentified",
    "not_observed",
    "invalid",
)
_COVERAGE_STATES = ("not_started", "partial", "complete")


def _confirmed_true(value: object) -> bool:
    """Accept only the literal boolean required by the physical precondition."""
    if value is not True:
        raise vol.Invalid("all modes off confirmation is required")
    return True


def _ui_integer(value: object) -> int:
    """Accept a strict integer or the exact decimal string emitted by selectors."""
    if type(value) is int:
        return value
    if isinstance(value, str) and _INTEGER_TEXT.fullmatch(value) is not None:
        return int(value)
    raise vol.Invalid("integer required")


_ENTRY_SCHEMA = vol.Schema(
    {vol.Required("config_entry_id"): _ENTRY_ID},
    extra=vol.PREVENT_EXTRA,
)
_START_SCHEMA = vol.Schema(
    {
        vol.Required("config_entry_id"): _ENTRY_ID,
        vol.Required("all_modes_off_confirmed"): _confirmed_true,
    },
    extra=vol.PREVENT_EXTRA,
)
_BEGIN_SCHEMA = vol.Schema(
    {
        vol.Required("config_entry_id"): _ENTRY_ID,
        vol.Required("experiment"): _EXPERIMENT,
        vol.Required("round"): vol.All(_ui_integer, vol.In((1, 2))),
        vol.Optional("source_level"): _ui_integer,
        vol.Optional("target_level"): _ui_integer,
        vol.Optional("source_temperature"): _ui_integer,
        vol.Optional("target_temperature"): _ui_integer,
    },
    extra=vol.PREVENT_EXTRA,
)


class _DiscoverySession(Protocol):
    """Exact v2 session surface accepted from one loaded runtime."""

    async def async_start(
        self,
        all_modes_off_confirmed: bool,
    ) -> DiscoveryProgress:
        """Start one session."""

    async def async_begin_step(
        self,
        request: DiscoveryStepRequest,
    ) -> DiscoveryProgress:
        """Begin one catalog cycle."""

    async def async_advance_step(self) -> DiscoveryProgress:
        """Advance one catalog phase."""

    async def async_finish(self) -> JsonObject:
        """Save and return one sanitized report."""

    async def async_cancel(self) -> DiscoveryProgress:
        """Cancel software collection."""

    async def async_stop(self) -> None:
        """Stop all session resources."""


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
    """Remove one route and unregister actions after the final loaded entry."""
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
        confirmed = cast(bool, call.data["all_modes_off_confirmed"])
        progress = await _call_session(lambda: session.async_start(confirmed))
        return progress.to_response()

    async def handle_begin(call: ServiceCall) -> ServiceResponse:
        session = _resolve_session(hass, call)
        try:
            request = build_step_request(
                experiment=cast(str, call.data["experiment"]),
                round_number=call.data["round"],
                source_level=call.data.get("source_level"),
                target_level=call.data.get("target_level"),
                source_temperature=call.data.get("source_temperature"),
                target_temperature=call.data.get("target_temperature"),
            )
        except ValueError:
            raise _service_error(DiscoveryInvalidParameterError.error_code) from None
        progress = await _call_session(lambda: session.async_begin_step(request))
        return progress.to_response()

    async def handle_advance(call: ServiceCall) -> ServiceResponse:
        session = _resolve_session(hass, call)
        progress = await _call_session(session.async_advance_step)
        return progress.to_response()

    async def handle_finish(call: ServiceCall) -> ServiceResponse:
        session = _resolve_session(hass, call)
        report = await _call_session(session.async_finish)
        return _finish_response(report)

    async def handle_cancel(call: ServiceCall) -> ServiceResponse:
        session = _resolve_session(hass, call)
        progress = await _call_session(session.async_cancel)
        return progress.to_response()

    handlers = {
        START_DISCOVERY: (handle_start, _START_SCHEMA),
        BEGIN_DISCOVERY_STEP: (handle_begin, _BEGIN_SCHEMA),
        ADVANCE_DISCOVERY_STEP: (handle_advance, _ENTRY_SCHEMA),
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


def _resolve_session(hass: HomeAssistant, call: ServiceCall) -> _DiscoverySession:
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
    required = (
        "async_start",
        "async_begin_step",
        "async_advance_step",
        "async_finish",
        "async_cancel",
        "async_stop",
    )
    if not all(callable(getattr(session, name, None)) for name in required):
        raise _service_error(DiscoveryInvalidTransitionError.error_code)
    return cast(_DiscoverySession, session)


async def _call_session[T](action: Callable[[], Awaitable[T]]) -> T:
    try:
        return await action()
    except DiscoveryError as err:
        raise _service_error(err.error_code) from None


def _finish_response(report: JsonObject) -> ServiceResponse:
    classifications = {classification: 0 for classification in _CLASSIFICATIONS}
    candidates = report.get("candidates")
    if isinstance(candidates, list):
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            classification = candidate.get("classification")
            if isinstance(classification, str) and classification in classifications:
                classifications[classification] += 1

    coverage_counts = {status: 0 for status in _COVERAGE_STATES}
    coverage = report.get("coverage")
    if isinstance(coverage, list):
        for row in coverage:
            if not isinstance(row, dict):
                continue
            status = row.get("status")
            if isinstance(status, str) and status in coverage_counts:
                coverage_counts[status] += 1

    return {
        "state": "idle",
        "message_code": "discovery_report_saved",
        "report_available": True,
        "confirmed_candidate_count": classifications["confirmed_candidate"],
        "ambiguous_count": classifications["ambiguous"],
        "observed_unidentified_count": classifications["observed_unidentified"],
        "not_observed_count": classifications["not_observed"],
        "invalid_count": classifications["invalid"],
        "coverage_not_started_count": coverage_counts["not_started"],
        "coverage_partial_count": coverage_counts["partial"],
        "coverage_complete_count": coverage_counts["complete"],
    }


def _service_error(error_code: str) -> ServiceValidationError:
    return ServiceValidationError(
        error_code,
        translation_domain=DOMAIN,
        translation_key=error_code,
    )
