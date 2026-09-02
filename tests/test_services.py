"""Tests for Config Entry-directed Q360 discovery Home Assistant actions."""

from __future__ import annotations

import importlib
from types import SimpleNamespace
from typing import Any

import pytest
import voluptuous as vol
from homeassistant.core import ServiceCall, SupportsResponse
from homeassistant.exceptions import ServiceValidationError

from custom_components.aupu_q360.const import DOMAIN
from custom_components.aupu_q360.errors import DiscoveryWssUnavailableError


def _module():  # type: ignore[no-untyped-def]
    return importlib.import_module("custom_components.aupu_q360.services")


class FakeServiceRegistry:
    """Record domain-level registration while keeping handlers directly callable."""

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
    """Expose the action API without transport, Store, or device state."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.start_error: Exception | None = None

    async def async_start(self) -> None:
        self.calls.append(("start",))
        if self.start_error is not None:
            raise self.start_error

    async def async_begin_step(
        self,
        capability: str,
        target: str,
        round_number: int,
    ) -> None:
        self.calls.append(("begin", capability, target, round_number))

    async def async_complete_step(self) -> None:
        self.calls.append(("complete",))

    async def async_finish(self) -> dict[str, object]:
        self.calls.append(("finish",))
        return {
            "candidates": [
                {"classification": "confirmed_candidate"},
                {"classification": "ambiguous"},
                {"classification": "ambiguous"},
                {"classification": "invalid"},
            ]
        }

    async def async_cancel(self) -> None:
        self.calls.append(("cancel",))


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
async def test_services_register_once_route_by_entry_and_unregister_last() -> None:
    """Catch duplicate domain handlers or actions reaching the wrong Config Entry."""
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
        module.COMPLETE_DISCOVERY_STEP,
        module.FINISH_DISCOVERY,
        module.CANCEL_DISCOVERY,
    }
    assert {name for domain, name in hass.services.handlers if domain == DOMAIN} == expected_names
    assert len(hass.services.register_calls) == 5
    assert set(hass.services.supports.values()) == {SupportsResponse.OPTIONAL}

    response = await _call(
        hass,
        module.BEGIN_DISCOVERY_STEP,
        {
            "config_entry_id": "entry-two",
            "capability": "heating",
            "target": "on",
            "round": 2,
        },
    )

    assert first_session.calls == []
    assert second_session.calls == [("begin", "heating", "on", 2)]
    assert response == {
        "state": "observing",
        "message_code": "discovery_ready_for_panel_action",
        "wait_seconds_min": 15,
        "wait_seconds_max": 30,
    }

    module.async_unregister_discovery_entry(hass, "entry-one")
    assert len(hass.services.handlers) == 5
    module.async_unregister_discovery_entry(hass, "entry-two")
    assert hass.services.handlers == {}
    assert len(hass.services.remove_calls) == 5


@pytest.mark.asyncio
async def test_begin_action_normalizes_ui_round_string_before_session() -> None:
    """Catch the select field's string value being rejected or passed through."""
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
            "capability": "heating",
            "target": "on",
            "round": "2",
        },
    )

    assert session.calls == [("begin", "heating", "on", 2)]


@pytest.mark.asyncio
async def test_all_action_responses_are_fixed_and_finish_returns_counts_only() -> None:
    """Catch raw report bodies or user data being echoed through service responses."""
    module = _module()
    hass = FakeHass()
    session = FakeSession()
    hass.config_entries.entries["entry-one"] = _entry("entry-one", session)
    module.async_register_discovery_entry(hass, "entry-one")
    base = {"config_entry_id": "entry-one"}

    assert await _call(hass, module.START_DISCOVERY, base) == {
        "state": "ready",
        "message_code": "discovery_ready_for_step",
    }
    assert await _call(hass, module.COMPLETE_DISCOVERY_STEP, base) == {
        "state": "ready",
        "message_code": "discovery_step_recorded",
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
    }
    assert "candidates" not in finish
    assert await _call(hass, module.CANCEL_DISCOVERY, base) == {
        "state": "idle",
        "message_code": "discovery_cancelled",
    }


@pytest.mark.asyncio
async def test_schemas_and_handlers_reject_unknown_unloaded_or_invalid_targets() -> None:
    """Catch free text, sibling integrations, or impossible experiment labels."""
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

    begin_schema = hass.services.schemas[(DOMAIN, module.BEGIN_DISCOVERY_STEP)]
    for invalid in (
        {},
        {"config_entry_id": "entry-one", "free_text": "private"},
        {
            "config_entry_id": "entry-one",
            "capability": "unknown",
            "target": "on",
            "round": 1,
        },
        {
            "config_entry_id": "entry-one",
            "capability": "heating",
            "target": "on",
            "round": 3,
        },
    ):
        with pytest.raises(vol.Invalid):
            begin_schema(invalid)

    for entry_id in ("missing", "entry-unloaded", "entry-sibling"):
        with pytest.raises(ServiceValidationError) as raised:
            await _call(
                hass,
                module.START_DISCOVERY,
                {"config_entry_id": entry_id},
            )
        assert raised.value.translation_key == "discovery_invalid_transition"

    with pytest.raises(ServiceValidationError) as raised:
        await _call(
            hass,
            module.BEGIN_DISCOVERY_STEP,
            {
                "config_entry_id": "entry-one",
                "capability": "heating",
                "target": "level_1",
                "round": 1,
            },
        )
    assert raised.value.translation_key == "discovery_invalid_transition"
    assert session.calls == []


@pytest.mark.asyncio
async def test_discovery_errors_become_translatable_fixed_service_errors() -> None:
    """Catch transport or payload context escaping from an action exception."""
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
            {"config_entry_id": "entry-one"},
        )

    assert str(raised.value) == "discovery_wss_unavailable"
    assert raised.value.translation_domain == DOMAIN
    assert raised.value.translation_key == "discovery_wss_unavailable"
