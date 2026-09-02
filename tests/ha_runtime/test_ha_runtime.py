"""Linux-only checks against Home Assistant's real flow and entry managers."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import aiohttp
import pytest
from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_USER, ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.restore_state import ATTR_RESTORED
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aupu_q360.api import AupuApiClient, WssCredentials
from custom_components.aupu_q360.const import DOMAIN
from custom_components.aupu_q360.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.aupu_q360.models import ApiResponse
from custom_components.aupu_q360.shadow import LightShadowUpdate
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


@dataclass(frozen=True, slots=True)
class _FakeMessage:
    type: aiohttp.WSMsgType
    data: bytes


class _FakeWebSocket:
    """Provide a complete synthetic MQTT handshake, then block locally."""

    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.messages: asyncio.Queue[_FakeMessage] = asyncio.Queue()
        self.receive_started = asyncio.Event()
        self.close_calls = 0
        for packet in (
            b"\x20\x02\x00\x00",
            b"\x90\x03\x00\x01\x00",
            b"\x90\x03\x00\x02\x00",
        ):
            self.messages.put_nowait(_FakeMessage(aiohttp.WSMsgType.BINARY, packet))

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(data)

    async def receive(self) -> _FakeMessage:
        self.receive_started.set()
        return await self.messages.get()

    async def close(self) -> None:
        self.close_calls += 1


class _FakeSession:
    """Keep the real WSS client on an in-memory aiohttp boundary."""

    def __init__(self, websocket: _FakeWebSocket) -> None:
        self.websocket = websocket
        self.calls: list[tuple[str, dict[str, str], tuple[str, ...]]] = []

    async def ws_connect(
        self,
        url: str,
        *,
        params: dict[str, str],
        protocols: tuple[str, ...],
    ) -> _FakeWebSocket:
        self.calls.append((url, dict(params), protocols))
        return self.websocket


class _ControlledSleep:
    """Record WSS waits while keeping each real task locally cancellable."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        await asyncio.Event().wait()


async def _wait_until(predicate: Callable[[], bool]) -> None:
    for _ in range(200):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was not reached")


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
    assert entities[0].domain == "light"
    entity_id = entities[0].entity_id

    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": entity_id},
        blocking=True,
    )
    assert calls == [True]
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == "on"

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
    websocket = _FakeWebSocket()
    session = _FakeSession(websocket)
    sleep = _ControlledSleep()
    original_init = AupuShadowWebSocket.__init__

    def init_with_fake_sleep(
        client: AupuShadowWebSocket,
        *args: object,
        **kwargs: object,
    ) -> None:
        kwargs["sleep"] = sleep
        original_init(client, *args, **kwargs)

    async def fake_get_wss_credentials(self: AupuApiClient) -> WssCredentials:
        del self
        return WssCredentials(
            authorizer_name="synthetic-authorizer",
            signature="synthetic-signature-1",
            token_key_name="synthetic-token-key",
        )

    monkeypatch.setattr(AupuShadowWebSocket, "__init__", init_with_fake_sleep)
    monkeypatch.setattr(AupuApiClient, "get_wss_credentials", fake_get_wss_credentials)
    monkeypatch.setattr(
        "custom_components.aupu_q360.async_get_clientsession",
        lambda _: session,
    )
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
    current = asyncio.current_task()
    before = {task for task in asyncio.all_tasks() if task is not current}

    assert await hass.config_entries.async_setup(entry.entry_id)
    await websocket.receive_started.wait()
    await _wait_until(lambda: sleep.delays == [30])
    running = {
        task.get_name()
        for task in asyncio.all_tasks()
        if task is not current and not task.done() and task.get_name().startswith("aupu_q360_wss")
    }
    assert running == {"aupu_q360_wss", "aupu_q360_wss_ping", "aupu_q360_wss_receive"}
    assert len(session.calls) == 1

    entities = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    assert sorted(entity.domain for entity in entities) == ["binary_sensor", "light"]
    channel = next(entity for entity in entities if entity.domain == "binary_sensor")
    light = next(entity for entity in entities if entity.domain == "light")
    channel_state = hass.states.get(channel.entity_id)
    assert channel_state is not None
    assert channel_state.state == "on"
    assert channel_state.attributes["healthy"] is False
    assert channel_state.attributes["state_stale"] is True

    entry.runtime_data.coordinator.async_apply_shadow_update(
        LightShadowUpdate(is_on=True, confirmed=True, source="reported")
    )
    await hass.async_block_till_done()
    light_state = hass.states.get(light.entity_id)
    assert light_state is not None
    assert light_state.state == "on"
    assert light_state.attributes["state_source"] == "reported"
    assert light_state.attributes["state_stale"] is False
    confirmed_at = light_state.attributes["last_confirmed_at"]
    assert isinstance(confirmed_at, str)
    parsed_confirmed_at = datetime.fromisoformat(confirmed_at)
    assert parsed_confirmed_at.tzinfo is not None
    assert parsed_confirmed_at.utcoffset() == UTC.utcoffset(parsed_confirmed_at)

    entry.runtime_data.coordinator.async_apply_wss_connection(False, False)
    await hass.async_block_till_done()
    stale_light_state = hass.states.get(light.entity_id)
    stale_channel_state = hass.states.get(channel.entity_id)
    assert stale_light_state is not None
    assert stale_light_state.state == "on"
    assert stale_light_state.attributes["state_source"] == "reported"
    assert stale_light_state.attributes["state_stale"] is True
    assert stale_light_state.attributes["last_confirmed_at"] == confirmed_at
    assert stale_channel_state is not None
    assert stale_channel_state.state == "off"
    assert stale_channel_state.attributes["healthy"] is False
    assert stale_channel_state.attributes["state_stale"] is True
    assert stale_channel_state.attributes["last_confirmed_at"] == confirmed_at

    await _unload(hass, entry)
    unloaded_light_state = hass.states.get(light.entity_id)
    unloaded_channel_state = hass.states.get(channel.entity_id)
    assert unloaded_light_state is not None
    assert unloaded_light_state.state == STATE_UNAVAILABLE
    assert unloaded_light_state.attributes[ATTR_RESTORED] is True
    assert unloaded_channel_state is not None
    assert unloaded_channel_state.state == STATE_UNAVAILABLE
    assert unloaded_channel_state.attributes[ATTR_RESTORED] is True
    await asyncio.sleep(0)
    after = {task for task in asyncio.all_tasks() if task is not current and not task.done()}
    assert after <= before
    assert websocket.sent[-1] == b"\xe0\x00"
    assert websocket.close_calls == 1


async def test_real_entry_manager_wss_to_https_only_removes_state_channel(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch a real mode reload leaving the prior channel registry or state behind."""
    websocket = _FakeWebSocket()
    session = _FakeSession(websocket)
    sleep = _ControlledSleep()
    original_init = AupuShadowWebSocket.__init__

    def init_with_fake_sleep(
        client: AupuShadowWebSocket,
        *args: object,
        **kwargs: object,
    ) -> None:
        kwargs["sleep"] = sleep
        original_init(client, *args, **kwargs)

    async def fake_get_wss_credentials(self: AupuApiClient) -> WssCredentials:
        del self
        return WssCredentials(
            authorizer_name="synthetic-authorizer",
            signature="synthetic-signature-1",
            token_key_name="synthetic-token-key",
        )

    monkeypatch.setattr(AupuShadowWebSocket, "__init__", init_with_fake_sleep)
    monkeypatch.setattr(AupuApiClient, "get_wss_credentials", fake_get_wss_credentials)
    monkeypatch.setattr(
        "custom_components.aupu_q360.async_get_clientsession",
        lambda _: session,
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="AUPU Q360",
        unique_id="synthetic-mode-entry",
        data=_entry_data(
            token=_jwt(expires_in=7 * 24 * 60 * 60, subject="synthetic-mode"),
            use_wss=True,
            user_uuid="synthetic-user-uuid",
        ),
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await websocket.receive_started.wait()
    await _wait_until(lambda: sleep.delays == [30])
    registry = er.async_get(hass)
    entities = er.async_entries_for_config_entry(registry, entry.entry_id)
    assert sorted(entity.domain for entity in entities) == ["binary_sensor", "light"]
    channel = next(entity for entity in entities if entity.domain == "binary_sensor")
    light = next(entity for entity in entities if entity.domain == "light")
    assert hass.states.get(channel.entity_id) is not None
    assert hass.states.get(light.entity_id) is not None

    https_only_data = dict(entry.data)
    https_only_data["use_wss"] = False
    assert hass.config_entries.async_update_entry(entry, data=https_only_data)
    await hass.async_block_till_done()

    remaining = er.async_entries_for_config_entry(registry, entry.entry_id)
    assert [entity.entity_id for entity in remaining] == [light.entity_id]
    assert registry.async_get(channel.entity_id) is None
    assert hass.states.get(channel.entity_id) is None
    assert registry.async_get(light.entity_id) is not None
    assert hass.states.get(light.entity_id) is not None
    running = {
        task.get_name()
        for task in asyncio.all_tasks()
        if not task.done() and task.get_name().startswith("aupu_q360_wss")
    }
    assert running == set()
    assert websocket.sent[-1] == b"\xe0\x00"
    assert websocket.close_calls == 1

    await _unload(hass, entry)
