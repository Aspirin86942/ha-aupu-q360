"""Tests for Config Entry-directed temporary Q360 probe actions."""

from __future__ import annotations

import importlib
from types import SimpleNamespace
from typing import Any

import pytest
import voluptuous as vol
from homeassistant.core import ServiceCall, SupportsResponse
from homeassistant.exceptions import ServiceValidationError

from custom_components.aupu_q360.const import DOMAIN
from custom_components.aupu_q360.probe import ProbeError, ProbeResponse


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


class FakeProbe:
    """Expose only the temporary probe Action and lifecycle API."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.error: ProbeError | None = None

    async def async_start(self) -> ProbeResponse:
        self.calls.append("start")
        if self.error is not None:
            raise self.error
        return {
            "state": "active",
            "message_code": "probe_started",
            "sample_count": 0,
            "changes": [],
        }

    async def async_sample(self) -> ProbeResponse:
        self.calls.append("sample")
        return {
            "state": "active",
            "message_code": "probe_sampled",
            "sample_count": 1,
            "changes": [{"path": "service/6/property/2", "before": 3, "after": 4}],
        }

    async def async_stop_probe(self) -> ProbeResponse:
        self.calls.append("stop_probe")
        return {
            "state": "inactive",
            "message_code": "probe_stopped",
            "sample_count": 1,
            "changes": [],
        }

    async def async_stop(self) -> None:
        self.calls.append("stop")


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


def _entry(entry_id: str, probe: FakeProbe | None = None) -> object:
    runtime = None if probe is None else SimpleNamespace(probe=probe)
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
async def test_probe_actions_route_by_config_entry_and_return_exact_payloads() -> None:
    """Catch a probe Action being routed to the wrong loaded Config Entry."""
    module = _module()
    first_probe = FakeProbe()
    second_probe = FakeProbe()
    hass = FakeHass()
    hass.config_entries.entries = {
        "entry-one": _entry("entry-one", first_probe),
        "entry-two": _entry("entry-two", second_probe),
    }
    module.async_register_probe_entry(hass, "entry-one")
    module.async_register_probe_entry(hass, "entry-two")

    responses = []
    for action in (module.START_PROBE, module.SAMPLE_PROBE, module.STOP_PROBE):
        responses.append(await _call(hass, action, {"config_entry_id": "entry-two"}))

    assert [response["message_code"] for response in responses] == [
        "probe_started",
        "probe_sampled",
        "probe_stopped",
    ]
    assert first_probe.calls == []
    assert second_probe.calls == ["start", "sample", "stop_probe"]


def test_probe_services_register_once_and_unregister_after_last_entry() -> None:
    """Catch duplicate registration or premature removal in a multi-entry domain."""
    module = _module()
    hass = FakeHass()

    module.async_register_probe_entry(hass, "entry-one")
    module.async_register_probe_entry(hass, "entry-two")

    expected_names = {
        module.START_PROBE,
        module.SAMPLE_PROBE,
        module.STOP_PROBE,
    }
    assert {name for domain, name in hass.services.handlers if domain == DOMAIN} == expected_names
    assert len(hass.services.register_calls) == 3
    assert set(hass.services.supports.values()) == {SupportsResponse.OPTIONAL}

    module.async_unregister_probe_entry(hass, "entry-one")
    assert len(hass.services.handlers) == 3
    module.async_unregister_probe_entry(hass, "entry-two")
    assert hass.services.handlers == {}
    assert len(hass.services.remove_calls) == 3


def test_probe_action_schemas_accept_only_config_entry_id() -> None:
    """Catch operator labels, experiment stages, or arbitrary values reaching a probe."""
    module = _module()
    hass = FakeHass()
    module.async_register_probe_entry(hass, "entry-one")

    for action in (module.START_PROBE, module.SAMPLE_PROBE, module.STOP_PROBE):
        schema = hass.services.schemas[(DOMAIN, action)]
        assert schema({"config_entry_id": "entry-one"}) == {"config_entry_id": "entry-one"}
        for invalid in (
            {},
            {"config_entry_id": ""},
            {"config_entry_id": "entry-one", "experiment": "night_light"},
            {"config_entry_id": "entry-one", "round": 1},
            {"config_entry_id": "entry-one", "label": "private"},
        ):
            with pytest.raises(vol.Invalid):
                schema(invalid)


@pytest.mark.asyncio
async def test_probe_actions_reject_unknown_unloaded_sibling_or_incomplete_runtime() -> None:
    """Catch Actions reaching an unregistered, sibling, or incomplete runtime."""
    module = _module()
    probe = FakeProbe()
    hass = FakeHass()
    hass.config_entries.entries = {
        "entry-one": _entry("entry-one", probe),
        "entry-unloaded": _entry("entry-unloaded", None),
        "entry-sibling": SimpleNamespace(
            entry_id="entry-sibling",
            domain="other_domain",
            runtime_data=SimpleNamespace(probe=probe),
        ),
    }
    module.async_register_probe_entry(hass, "entry-one")

    for entry_id in ("missing", "entry-unloaded", "entry-sibling"):
        with pytest.raises(ServiceValidationError) as raised:
            await _call(
                hass,
                module.START_PROBE,
                {"config_entry_id": entry_id},
            )
        assert raised.value.translation_key == "probe_inactive"


@pytest.mark.asyncio
async def test_probe_errors_become_translatable_fixed_service_errors() -> None:
    """Catch transport or payload context escaping from an Action exception."""
    module = _module()
    probe = FakeProbe()
    probe.error = ProbeError("probe_wss_unavailable")
    hass = FakeHass()
    hass.config_entries.entries["entry-one"] = _entry("entry-one", probe)
    module.async_register_probe_entry(hass, "entry-one")

    with pytest.raises(ServiceValidationError) as raised:
        await _call(
            hass,
            module.START_PROBE,
            {"config_entry_id": "entry-one"},
        )

    assert str(raised.value) == "probe_wss_unavailable"
    assert raised.value.translation_domain == DOMAIN
    assert raised.value.translation_key == "probe_wss_unavailable"
