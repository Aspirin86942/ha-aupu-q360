"""Tests for Config Entry-directed Q360 v2 discovery actions."""

from __future__ import annotations

import importlib
from types import SimpleNamespace
from typing import Any

import pytest
import voluptuous as vol
from homeassistant.core import ServiceCall, SupportsResponse
from homeassistant.exceptions import ServiceValidationError

from custom_components.aupu_q360.const import DOMAIN
from custom_components.aupu_q360.discovery_models import (
    DiscoveryPhase,
    DiscoveryProgress,
    DiscoveryState,
    DiscoveryStepRequest,
)
from custom_components.aupu_q360.errors import DiscoveryWssUnavailableError


def _module():  # type: ignore[no-untyped-def]
    return importlib.import_module("custom_components.aupu_q360.services")


class FakeServiceRegistry:
    """Record domain registration while keeping handlers directly callable."""

    def __init__(self) -> None:
        self.handlers: dict[tuple[str, str], Any] = {}
        self.schemas: dict[tuple[str, str], vol.Schema] = {}
        self.supports: dict[tuple[str, str], SupportsResponse] = {}
        self.register_calls: list[tuple[str, str]] = []
        self.remove_calls: list[tuple[str, str]] = []

    def async_register(
        self,
        domain: str,
        service: str,
        handler: Any,
        schema: vol.Schema,
        supports_response: SupportsResponse,
    ) -> None:
        key = (domain, service)
        self.register_calls.append(key)
        self.handlers[key] = handler
        self.schemas[key] = schema
        self.supports[key] = supports_response

    def async_remove(self, domain: str, service: str) -> None:
        key = (domain, service)
        self.remove_calls.append(key)
        self.handlers.pop(key, None)
        self.schemas.pop(key, None)
        self.supports.pop(key, None)


class FakeSession:
    """Expose the exact v2 Action API without transport or device state."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.start_error: Exception | None = None

    async def async_start(
        self,
        all_modes_off_confirmed: bool,
    ) -> DiscoveryProgress:
        self.calls.append(("start", all_modes_off_confirmed))
        if self.start_error is not None:
            raise self.start_error
        return DiscoveryProgress(
            state=DiscoveryState.READY,
            message_code="discovery_ready_for_step",
        )

    async def async_begin_step(
        self,
        request: DiscoveryStepRequest,
    ) -> DiscoveryProgress:
        self.calls.append(("begin", request))
        return DiscoveryProgress(
            state=DiscoveryState.AWAITING_OPERATOR,
            message_code="discovery_prompt_mode_on",
            phase=DiscoveryPhase.MODE_ON,
        )

    async def async_advance_step(self) -> DiscoveryProgress:
        self.calls.append(("advance",))
        return DiscoveryProgress(
            state=DiscoveryState.READY,
            message_code="discovery_cycle_recorded",
            completed_cycle_count=1,
        )

    async def async_finish(self) -> dict[str, object]:
        self.calls.append(("finish",))
        return {
            "candidates": [
                {"classification": "confirmed_candidate"},
                {"classification": "ambiguous"},
                {"classification": "ambiguous"},
                {"classification": "invalid"},
            ],
            "coverage": [
                {"status": "not_started"},
                {"status": "partial"},
                {"status": "partial"},
                {"status": "complete"},
            ],
        }

    async def async_cancel(self) -> DiscoveryProgress:
        self.calls.append(("cancel",))
        return DiscoveryProgress(
            state=DiscoveryState.CANCELLED,
            message_code="discovery_manual_restore_required",
            manual_restore_required=True,
        )

    async def async_stop(self) -> None:
        self.calls.append(("stop",))


class FakeConfigEntries:
    def __init__(self) -> None:
        self.entries: dict[str, object] = {}

    def async_get_entry(self, entry_id: str) -> object | None:
        return self.entries.get(entry_id)


class FakeHass:
    def __init__(self) -> None:
        self.data: dict[str, object] = {}
        self.services = FakeServiceRegistry()
        self.config_entries = FakeConfigEntries()


def _entry(entry_id: str, session: FakeSession | None = None) -> object:
    runtime = None if session is None else SimpleNamespace(discovery_session=session)
    return SimpleNamespace(
        entry_id=entry_id,
        domain=DOMAIN,
        runtime_data=runtime,
    )


async def _call(
    hass: FakeHass,
    service: str,
    data: dict[str, object],
) -> dict[str, object]:
    key = (DOMAIN, service)
    validated = hass.services.schemas[key](data)
    call = ServiceCall(
        hass=hass,
        domain=DOMAIN,
        service=service,
        data=validated,
        return_response=True,
    )
    return await hass.services.handlers[key](call)


@pytest.mark.asyncio
async def test_services_register_once_route_v2_requests_and_unregister_last() -> None:
    """Catch duplicate handlers, v1 registration, or entry misrouting."""
    module = _module()
    hass = FakeHass()
    first_session = FakeSession()
    second_session = FakeSession()
    hass.config_entries.entries = {
        "entry-one": _entry("entry-one", first_session),
        "entry-two": _entry("entry-two", second_session),
    }

    module.async_register_discovery_entry(hass, "entry-one")
    module.async_register_discovery_entry(hass, "entry-two")

    expected_names = {
        module.START_DISCOVERY,
        module.BEGIN_DISCOVERY_STEP,
        module.ADVANCE_DISCOVERY_STEP,
        module.FINISH_DISCOVERY,
        module.CANCEL_DISCOVERY,
    }
    assert {name for domain, name in hass.services.handlers if domain == DOMAIN} == expected_names
    assert "complete_discovery_step" not in expected_names
    assert len(hass.services.register_calls) == 5
    assert set(hass.services.supports.values()) == {SupportsResponse.OPTIONAL}

    response = await _call(
        hass,
        module.BEGIN_DISCOVERY_STEP,
        {
            "config_entry_id": "entry-two",
            "experiment": "night_light",
            "round": 2,
        },
    )

    assert first_session.calls == []
    call_name, request = second_session.calls[0]
    assert call_name == "begin"
    assert isinstance(request, DiscoveryStepRequest)
    assert request.cycle_id == "night_light:2"
    assert response == {
        "state": "awaiting_operator",
        "message_code": "discovery_prompt_mode_on",
        "phase": "mode_on",
        "completed_cycle_count": 0,
        "manual_restore_required": False,
    }

    module.async_unregister_discovery_entry(hass, "entry-one")
    assert len(hass.services.handlers) == 5
    module.async_unregister_discovery_entry(hass, "entry-two")
    assert hass.services.handlers == {}
    assert len(hass.services.remove_calls) == 5


@pytest.mark.asyncio
async def test_begin_action_builds_parameter_request_from_ui_integer_strings() -> None:
    """Catch UI values bypassing the catalog or reaching the session as free-form fields."""
    module = _module()
    hass = FakeHass()
    session = FakeSession()
    hass.config_entries.entries["entry-one"] = _entry("entry-one", session)
    module.async_register_discovery_entry(hass, "entry-one")

    await _call(
        hass,
        module.BEGIN_DISCOVERY_STEP,
        {
            "config_entry_id": "entry-one",
            "experiment": "global_fan_level",
            "round": "2",
            "source_level": "3",
            "target_level": "5",
        },
    )

    _, request = session.calls[0]
    assert isinstance(request, DiscoveryStepRequest)
    assert request.cycle_id == "global_fan_level:3:5:2"
    assert request.source_level == 3
    assert request.target_level == 5


@pytest.mark.asyncio
async def test_all_action_responses_are_fixed_and_finish_returns_counts_only() -> None:
    """Catch reports, identifiers, or restoration details leaking through responses."""
    module = _module()
    hass = FakeHass()
    session = FakeSession()
    hass.config_entries.entries["entry-one"] = _entry("entry-one", session)
    module.async_register_discovery_entry(hass, "entry-one")
    base = {"config_entry_id": "entry-one"}

    assert await _call(
        hass,
        module.START_DISCOVERY,
        {**base, "all_modes_off_confirmed": True},
    ) == {
        "state": "ready",
        "message_code": "discovery_ready_for_step",
        "completed_cycle_count": 0,
        "manual_restore_required": False,
    }
    assert await _call(hass, module.ADVANCE_DISCOVERY_STEP, base) == {
        "state": "ready",
        "message_code": "discovery_cycle_recorded",
        "completed_cycle_count": 1,
        "manual_restore_required": False,
    }
    finish = await _call(hass, module.FINISH_DISCOVERY, base)
    assert finish == {
        "state": "idle",
        "message_code": "discovery_report_saved",
        "report_available": True,
        "confirmed_candidate_count": 1,
        "ambiguous_count": 2,
        "observed_unidentified_count": 0,
        "not_observed_count": 0,
        "invalid_count": 1,
        "coverage_not_started_count": 1,
        "coverage_partial_count": 2,
        "coverage_complete_count": 1,
    }
    assert "candidates" not in finish
    assert "coverage" not in finish
    assert await _call(hass, module.CANCEL_DISCOVERY, base) == {
        "state": "cancelled",
        "message_code": "discovery_manual_restore_required",
        "completed_cycle_count": 0,
        "manual_restore_required": True,
    }


@pytest.mark.asyncio
async def test_schemas_and_catalog_reject_unknown_extra_or_invalid_field_matrices() -> None:
    """Catch free text, caller phases, false confirmation, or illegal parameter matrices."""
    module = _module()
    hass = FakeHass()
    session = FakeSession()
    hass.config_entries.entries["entry-one"] = _entry("entry-one", session)
    module.async_register_discovery_entry(hass, "entry-one")

    start_schema = hass.services.schemas[(DOMAIN, module.START_DISCOVERY)]
    for invalid in (
        {},
        {"config_entry_id": "entry-one"},
        {"config_entry_id": "entry-one", "all_modes_off_confirmed": False},
        {"config_entry_id": "entry-one", "all_modes_off_confirmed": 1},
        {
            "config_entry_id": "entry-one",
            "all_modes_off_confirmed": True,
            "archive_path": "/private",
        },
    ):
        with pytest.raises(vol.Invalid):
            start_schema(invalid)

    begin_schema = hass.services.schemas[(DOMAIN, module.BEGIN_DISCOVERY_STEP)]
    for invalid in (
        {},
        {"config_entry_id": "entry-one", "free_text": "private"},
        {
            "config_entry_id": "entry-one",
            "experiment": "unknown",
            "round": 1,
        },
        {
            "config_entry_id": "entry-one",
            "experiment": "night_light",
            "round": 3,
        },
        {
            "config_entry_id": "entry-one",
            "experiment": "night_light",
            "round": 1,
            "phase": "mode_on",
        },
    ):
        with pytest.raises(vol.Invalid):
            begin_schema(invalid)

    invalid_matrices = (
        {"experiment": "night_light", "round": 1, "source_level": 3},
        {
            "experiment": "global_fan_level",
            "round": 1,
            "source_level": 3,
            "target_level": 3,
        },
        {
            "experiment": "ai_target_temperature",
            "round": 1,
            "source_temperature": 35,
            "target_temperature": 37,
        },
    )
    for fields in invalid_matrices:
        with pytest.raises(ServiceValidationError) as raised:
            await _call(
                hass,
                module.BEGIN_DISCOVERY_STEP,
                {"config_entry_id": "entry-one", **fields},
            )
        assert raised.value.translation_key == "discovery_invalid_parameter"
    assert session.calls == []


@pytest.mark.asyncio
async def test_actions_reject_unknown_unloaded_sibling_or_incomplete_sessions() -> None:
    """Catch actions reaching a missing Config Entry or a non-v2 runtime object."""
    module = _module()
    hass = FakeHass()
    session = FakeSession()
    hass.config_entries.entries = {
        "entry-one": _entry("entry-one", session),
        "entry-unloaded": _entry("entry-unloaded", None),
        "entry-sibling": SimpleNamespace(
            entry_id="entry-sibling",
            domain="other_domain",
            runtime_data=SimpleNamespace(discovery_session=session),
        ),
    }
    module.async_register_discovery_entry(hass, "entry-one")

    for entry_id in ("missing", "entry-unloaded", "entry-sibling"):
        with pytest.raises(ServiceValidationError) as raised:
            await _call(
                hass,
                module.START_DISCOVERY,
                {
                    "config_entry_id": entry_id,
                    "all_modes_off_confirmed": True,
                },
            )
        assert raised.value.translation_key == "discovery_invalid_transition"


@pytest.mark.asyncio
async def test_discovery_errors_become_translatable_fixed_service_errors() -> None:
    """Catch transport or payload context escaping from an Action exception."""
    module = _module()
    hass = FakeHass()
    session = FakeSession()
    session.start_error = DiscoveryWssUnavailableError()
    hass.config_entries.entries["entry-one"] = _entry("entry-one", session)
    module.async_register_discovery_entry(hass, "entry-one")

    with pytest.raises(ServiceValidationError) as raised:
        await _call(
            hass,
            module.START_DISCOVERY,
            {
                "config_entry_id": "entry-one",
                "all_modes_off_confirmed": True,
            },
        )

    assert str(raised.value) == "discovery_wss_unavailable"
    assert raised.value.translation_domain == DOMAIN
    assert raised.value.translation_key == "discovery_wss_unavailable"
