"""Linux-only checks against Home Assistant's real flow and entry managers."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import Any

import pytest
from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_USER, ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aupu_q360.api import AupuApiClient
from custom_components.aupu_q360.const import DOMAIN
from custom_components.aupu_q360.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.aupu_q360.models import ApiResponse
from custom_components.aupu_q360.wss import AupuShadowWebSocket

pytestmark = [
    pytest.mark.ha_runtime,
    pytest.mark.usefixtures("enable_custom_integrations"),
]

SYNTHETIC_SIGNER = {
    "app_key": "synthetic-app-key",
    "key_prefix": "synthetic-prefix",
    "package_name": "synthetic.package",
    "key_suffix": "synthetic-suffix",
    "sdk_version": "synthetic-sdk",
    "message_prefix": "synthetic-message",
    "sdk_label": "synthetic-sdk-label",
    "type_timestamp_label": "synthetic-timestamp-label",
    "header_prefix": "synthetic-header",
    "header_sep_1": "synthetic-separator-one",
    "header_sep_2": "synthetic-separator-two",
    "signature_label": "synthetic-signature-label",
}


def _jwt(*, expires_in: int, subject: str) -> str:
    """Build an unsigned synthetic JWT-shaped value for local expiry parsing."""

    def encode(value: object) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return (
        f"{encode({'alg': 'none'})}."
        f"{encode({'exp': int(time.time()) + expires_in, 'sub': subject})}."
        "synthetic-signature"
    )


def _user_input(*, token: str, use_wss: bool = False) -> dict[str, object]:
    return {
        "signer_json": json.dumps(SYNTHETIC_SIGNER),
        "token": token,
        "did": "123456789",
        "tag": "synthetic-tag",
        "use_wss": use_wss,
    }


def _entry_data(
    *, token: str, use_wss: bool = False, user_uuid: str | None = None
) -> dict[str, object]:
    data: dict[str, object] = {
        "signer": dict(SYNTHETIC_SIGNER),
        "token": token,
        "did": "123456789",
        "tag": "synthetic-tag",
        "use_wss": use_wss,
    }
    if user_uuid is not None:
        data["user_uuid"] = user_uuid
    return data


async def _unload(hass: HomeAssistant, entry: ConfigEntry[Any]) -> None:
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_real_flow_managers_complete_user_options_and_manual_reauth(
    hass: HomeAssistant,
) -> None:
    """Catch flow registration or atomic update behavior diverging in real HA."""
    first_token = _jwt(expires_in=7 * 24 * 60 * 60, subject="synthetic-initial")
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USER},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        _user_input(token=first_token),
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    assert isinstance(entry, ConfigEntry)
    await hass.async_block_till_done()

    options_result = await hass.config_entries.options.async_init(entry.entry_id)
    assert options_result["type"] is FlowResultType.FORM
    assert options_result["step_id"] == "init"
    options_token = _jwt(
        expires_in=8 * 24 * 60 * 60,
        subject="synthetic-options",
    )
    options_result = await hass.config_entries.options.async_configure(
        options_result["flow_id"],
        {"token": options_token, "phone": "", "use_wss": False},
    )
    assert options_result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.data["token"] == options_token

    reauth_result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
        data=dict(entry.data),
    )
    assert reauth_result["type"] is FlowResultType.FORM
    assert reauth_result["step_id"] == "reauth_method"
    reauth_result = await hass.config_entries.flow.async_configure(
        reauth_result["flow_id"],
        {"method": "manual_token"},
    )
    assert reauth_result["type"] is FlowResultType.FORM
    assert reauth_result["step_id"] == "reauth_manual_token"

    reauth_token = _jwt(
        expires_in=9 * 24 * 60 * 60,
        subject="synthetic-reauth",
    )
    reauth_result = await hass.config_entries.flow.async_configure(
        reauth_result["flow_id"],
        {"token": reauth_token},
    )
    assert reauth_result["type"] is FlowResultType.ABORT
    assert reauth_result["reason"] == "reauth_successful"
    await hass.async_block_till_done()
    assert entry.data["token"] == reauth_token

    await _unload(hass, entry)


async def test_real_entry_manager_exposes_one_light_service_and_diagnostics(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch setup, service routing, diagnostics, or unload manager regressions."""
    calls: list[bool] = []

    async def fake_set_light(self: AupuApiClient, is_on: bool) -> ApiResponse:
        del self
        calls.append(is_on)
        return ApiResponse(status=200, result={}, timestamp=0)

    monkeypatch.setattr(AupuApiClient, "set_light", fake_set_light)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="AUPU Q360",
        unique_id="synthetic-unique-id",
        data=_entry_data(token=_jwt(expires_in=7 * 24 * 60 * 60, subject="synthetic-light")),
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    entities = er.async_entries_for_config_entry(registry, entry.entry_id)
    assert len(entities) == 1
    entity_id = entities[0].entity_id

    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": entity_id},
        blocking=True,
    )
    assert calls == [True]
    assert hass.states[entity_id].state == "on"

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert set(diagnostics) == {
        "integration_version",
        "authentication_expiry_bucket",
        "wss_enabled",
        "wss_connected",
        "wss_healthy",
        "last_error_code",
        "light_state_source",
        "assumed_state",
    }
    assert diagnostics["last_error_code"] == "none"
    assert diagnostics["light_state_source"] == "command"

    await _unload(hass, entry)
    assert not hasattr(entry, "runtime_data")


async def test_real_entry_reload_creates_and_clears_expiring_repair(
    hass: HomeAssistant,
) -> None:
    """Catch Repair state surviving after a real config-entry reload recovers JWT."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="AUPU Q360",
        unique_id="synthetic-repair-entry",
        data=_entry_data(token=_jwt(expires_in=60 * 60, subject="synthetic-expiring")),
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    registry = ir.async_get(hass)
    issue_id = f"{entry.entry_id}_jwt_expiring"
    assert registry.async_get_issue(DOMAIN, issue_id) is not None

    hass.config_entries.async_update_entry(
        entry,
        data=_entry_data(token=_jwt(expires_in=7 * 24 * 60 * 60, subject="synthetic-ready")),
    )
    await hass.async_block_till_done()
    assert registry.async_get_issue(DOMAIN, issue_id) is None

    await _unload(hass, entry)


async def test_real_entry_manager_starts_and_stops_fake_wss_without_task_leak(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch real setup/unload losing ownership of the optional WSS lifecycle."""
    starts: list[AupuShadowWebSocket] = []
    stops: list[AupuShadowWebSocket] = []

    async def fake_start(client: AupuShadowWebSocket) -> None:
        starts.append(client)

    async def fake_stop(client: AupuShadowWebSocket) -> None:
        stops.append(client)

    monkeypatch.setattr(AupuShadowWebSocket, "async_start", fake_start)
    monkeypatch.setattr(AupuShadowWebSocket, "async_stop", fake_stop)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="AUPU Q360",
        unique_id="synthetic-wss-entry",
        data=_entry_data(
            token=_jwt(expires_in=7 * 24 * 60 * 60, subject="synthetic-wss"),
            use_wss=True,
            user_uuid="synthetic-user-uuid",
        ),
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert len(starts) == 1
    assert entry.runtime_data.coordinator is not None

    await _unload(hass, entry)
    assert stops == starts
    assert not any(
        task.get_name() == "aupu_q360_wss" and not task.done() for task in asyncio.all_tasks()
    )
