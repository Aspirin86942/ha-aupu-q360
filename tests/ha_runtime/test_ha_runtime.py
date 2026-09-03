"""Linux-only checks against Home Assistant's real flow and entry managers."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_USER, ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.restore_state import ATTR_RESTORED
from homeassistant.helpers.service import _SERVICES_SCHEMA
from homeassistant.util.yaml import load_yaml_dict
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aupu_q360.api import AupuApiClient, WssCredentials
from custom_components.aupu_q360.const import DOMAIN
from custom_components.aupu_q360.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.aupu_q360.discovery_store import DiscoveryReportStore
from custom_components.aupu_q360.errors import DiscoveryRawArchiveUnavailableError
from custom_components.aupu_q360.models import ApiResponse
from custom_components.aupu_q360.mqtt_codec import decode_packets, encode_publish
from custom_components.aupu_q360.raw_discovery_archive import RawDiscoveryArchive
from custom_components.aupu_q360.services import (
    ADVANCE_DISCOVERY_STEP,
    BEGIN_DISCOVERY_STEP,
    CANCEL_DISCOVERY,
    FINISH_DISCOVERY,
    START_DISCOVERY,
)
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
        self.closed = False
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
        self.closed = True

    def queue_binary(self, data: bytes) -> None:
        self.messages.put_nowait(_FakeMessage(aiohttp.WSMsgType.BINARY, data))


class _FakeSession:
    """Keep the real WSS client on an in-memory aiohttp boundary."""

    def __init__(self, websocket: _FakeWebSocket | list[_FakeWebSocket]) -> None:
        self.websockets = list(websocket) if isinstance(websocket, list) else [websocket]
        self.calls: list[tuple[str, dict[str, str], tuple[str, ...]]] = []

    async def ws_connect(
        self,
        url: str,
        *,
        params: dict[str, str],
        protocols: tuple[str, ...],
    ) -> _FakeWebSocket:
        self.calls.append((url, dict(params), protocols))
        if len(self.websockets) > 1:
            return self.websockets.pop(0)
        return self.websockets[0]


class _ControlledSleep:
    """Record WSS waits while keeping each real task locally cancellable."""

    def __init__(self) -> None:
        self.delays: list[float] = []
        self.waiters: asyncio.Queue[asyncio.Future[None]] = asyncio.Queue()

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        waiter = asyncio.get_running_loop().create_future()
        self.waiters.put_nowait(waiter)
        await waiter

    async def release_next(self) -> None:
        while True:
            waiter = await self.waiters.get()
            if waiter.done():
                continue
            waiter.set_result(None)
            await asyncio.sleep(0)
            return


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
    *,
    token: str,
    use_wss: bool = False,
    user_uuid: str | None = None,
    raw_archive_enabled: bool = False,
) -> dict[str, object]:
    data: dict[str, object] = {
        "signer": dict(SYNTHETIC_SIGNER),
        "token": token,
        "did": "123456789",
        "tag": "synthetic-tag",
        "use_wss": use_wss,
        "raw_archive_enabled": raw_archive_enabled,
    }
    if user_uuid is not None:
        data["user_uuid"] = user_uuid
    return data


async def _unload(hass: HomeAssistant, entry: ConfigEntry[Any]) -> None:
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


def _shadow_state(
    is_on: bool = False,
    *,
    night_light: bool = False,
    ventilation: bool = False,
    fan_level: int = 3,
    ai_warmth: bool = False,
    temperature: int = 35,
) -> dict[str, object]:
    return {
        "reported": {
            "123456789": {
                "2": {"properties": {"1": is_on}},
                "5": {"properties": {"1": night_light}},
                "6": {"properties": {"1": ventilation, "2": fan_level}},
                "7": {"properties": {"1": ai_warmth, "2": temperature}},
                "8": {"properties": {"1": "synthetic-private-raw-marker"}},
            }
        }
    }


async def _call_discovery_snapshot_action(
    hass: HomeAssistant,
    websocket: _FakeWebSocket,
    service: str,
    data: dict[str, object],
    state: dict[str, object],
) -> dict[str, Any]:
    sent_before = len(websocket.sent)
    task = asyncio.create_task(
        hass.services.async_call(
            DOMAIN,
            service,
            data,
            blocking=True,
            return_response=True,
        )
    )
    await _wait_until(lambda: len(websocket.sent) > sent_before)
    packet = decode_packets(websocket.sent[-1])[0]
    assert packet.topic == "$aws/things/123456789/shadow/get"
    request = json.loads(packet.payload)
    client_token = request["clientToken"]
    assert isinstance(client_token, str)
    response_payload = json.dumps(
        {"clientToken": client_token, "state": state},
        separators=(",", ":"),
    ).encode()
    websocket.queue_binary(
        encode_publish(
            "$aws/things/123456789/shadow/get/accepted",
            response_payload,
        )
    )
    response = await task
    assert isinstance(response, dict)
    return response


async def _queue_shadow_update(
    hass: HomeAssistant,
    websocket: _FakeWebSocket,
    state: dict[str, object],
) -> None:
    websocket.queue_binary(
        encode_publish(
            "$aws/things/123456789/shadow/update/accepted",
            json.dumps({"state": state}, separators=(",", ":")).encode(),
        )
    )
    await hass.async_block_till_done()


def test_services_yaml_matches_home_assistant_runtime_schema() -> None:
    """Catch invalid service selectors hiding every Q360 action description."""
    services_path = Path(__file__).parents[2] / "custom_components/aupu_q360/services.yaml"
    descriptions = _SERVICES_SCHEMA(load_yaml_dict(str(services_path)))

    assert set(descriptions) == {
        "start_discovery",
        "begin_discovery_step",
        "advance_discovery_step",
        "finish_discovery",
        "cancel_discovery",
    }


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
        "state_discovery",
    }
    assert diagnostics["last_error_code"] == "none"
    assert diagnostics["light_state_source"] == "command"
    assert diagnostics["state_discovery"] == {"report_available": False}

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


async def test_real_services_complete_sanitized_discovery_and_remove_private_store(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Run the complete v2 matrix through real services, archive, Store, and reload."""
    initial_websocket = _FakeWebSocket()
    websocket = _FakeWebSocket()
    reload_websocket = _FakeWebSocket()
    session = _FakeSession([initial_websocket, websocket, reload_websocket])
    sleep = _ControlledSleep()
    original_init = AupuShadowWebSocket.__init__
    archive_root = tmp_path / "private-archive"
    archive_root.mkdir(mode=0o700)
    archive_root.chmod(0o700)
    original_archive_open = RawDiscoveryArchive.async_open.__func__
    control_calls: list[bool] = []
    credential_calls = 0

    def init_with_fake_sleep(
        client: AupuShadowWebSocket,
        *args: object,
        **kwargs: object,
    ) -> None:
        kwargs["sleep"] = sleep
        original_init(client, *args, **kwargs)

    async def fake_get_wss_credentials(self: AupuApiClient) -> WssCredentials:
        del self
        nonlocal credential_calls
        credential_calls += 1
        return WssCredentials(
            authorizer_name="synthetic-authorizer",
            signature=f"synthetic-signature-{credential_calls}",
            token_key_name="synthetic-token-key",
        )

    async def fake_set_light(self: AupuApiClient, is_on: bool) -> ApiResponse:
        del self
        control_calls.append(is_on)
        raise AssertionError("discovery attempted an HTTPS control call")

    async def open_temporary_archive(
        cls: type[RawDiscoveryArchive],
        on_failure: Callable[[str], None],
        **kwargs: object,
    ) -> RawDiscoveryArchive:
        assert kwargs == {}
        return await original_archive_open(cls, on_failure, root=archive_root)

    monkeypatch.setattr(AupuShadowWebSocket, "__init__", init_with_fake_sleep)
    monkeypatch.setattr(AupuApiClient, "get_wss_credentials", fake_get_wss_credentials)
    monkeypatch.setattr(AupuApiClient, "set_light", fake_set_light)
    monkeypatch.setattr(
        RawDiscoveryArchive,
        "async_open",
        classmethod(open_temporary_archive),
    )
    monkeypatch.setattr(
        "custom_components.aupu_q360.async_get_clientsession",
        lambda _: session,
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="AUPU Q360",
        unique_id="synthetic-discovery-entry",
        data=_entry_data(
            token=_jwt(expires_in=7 * 24 * 60 * 60, subject="synthetic-discovery"),
            use_wss=True,
            user_uuid="synthetic-user-uuid",
            raw_archive_enabled=True,
        ),
    )
    entry.add_to_hass(hass)
    current = asyncio.current_task()
    tasks_before = {task for task in asyncio.all_tasks() if task is not current}

    assert await hass.config_entries.async_setup(entry.entry_id)
    await initial_websocket.receive_started.wait()
    await _wait_until(lambda: sleep.delays == [30])
    for service in (
        START_DISCOVERY,
        BEGIN_DISCOVERY_STEP,
        ADVANCE_DISCOVERY_STEP,
        FINISH_DISCOVERY,
        CANCEL_DISCOVERY,
    ):
        assert hass.services.has_service(DOMAIN, service)

    base = {"config_entry_id": entry.entry_id}
    baseline = _shadow_state()
    start_task = asyncio.create_task(
        hass.services.async_call(
            DOMAIN,
            START_DISCOVERY,
            {**base, "all_modes_off_confirmed": True},
            blocking=True,
            return_response=True,
        )
    )
    await _wait_until(lambda: len(session.calls) == 2)
    await _wait_until(lambda: len(websocket.sent) == 4)
    assert initial_websocket.sent[-1] == b"\xe0\x00"
    assert initial_websocket.close_calls == 1
    assert initial_websocket.closed is True
    assert credential_calls == 2
    assert start_task.done() is False
    assert all(
        json.loads(packet.payload).get("clientToken") is None
        for raw in websocket.sent
        if (packet := decode_packets(raw)[0]).packet_type.name == "PUBLISH"
    )

    await sleep.release_next()
    await _wait_until(lambda: websocket.sent[-1] == b"\xc0\x00")
    websocket.queue_binary(b"\xd0\x00")
    await asyncio.sleep(0.01)
    await _wait_until(lambda: len(websocket.sent) == 6)
    request_packet = decode_packets(websocket.sent[-1])[0]
    request = json.loads(request_packet.payload)
    client_token = request["clientToken"]
    websocket.queue_binary(
        encode_publish(
            "$aws/things/123456789/shadow/get/accepted",
            json.dumps(
                {"clientToken": client_token, "state": baseline},
                separators=(",", ":"),
            ).encode(),
        )
    )
    start_response = await start_task
    assert isinstance(start_response, dict)
    assert start_response == {
        "state": "ready",
        "message_code": "discovery_ready_for_step",
        "completed_cycle_count": 0,
        "manual_restore_required": False,
    }

    completed_cycles = 0

    async def begin_cycle(
        experiment: str,
        round_number: int,
        **parameters: int,
    ) -> dict[str, Any]:
        return await _call_discovery_snapshot_action(
            hass,
            websocket,
            BEGIN_DISCOVERY_STEP,
            {
                **base,
                "experiment": experiment,
                "round": round_number,
                **parameters,
            },
            baseline,
        )

    async def advance_cycle(state: dict[str, object]) -> dict[str, Any]:
        return await _call_discovery_snapshot_action(
            hass,
            websocket,
            ADVANCE_DISCOVERY_STEP,
            base,
            state,
        )

    for round_number in (1, 2):
        begin_response = await begin_cycle("idle_environment", round_number)
        assert begin_response["phase"] == "idle_observation"
        completed_cycles += 1
        idle_response = await advance_cycle(baseline)
        assert idle_response["message_code"] == "discovery_cycle_recorded"
        assert idle_response["completed_cycle_count"] == completed_cycles

    for round_number in (1, 2):
        begin_response = await begin_cycle("night_light", round_number)
        assert begin_response["phase"] == "mode_on"
        panel_on = _shadow_state(is_on=True, night_light=True)
        await _queue_shadow_update(hass, websocket, panel_on)
        restore_prompt = await advance_cycle(panel_on)
        assert restore_prompt["phase"] == "mode_restore"
        assert restore_prompt["manual_restore_required"] is True
        if round_number == 1:
            restore_required = await advance_cycle(panel_on)
            assert restore_required["state"] == "restore_required"
            assert restore_required["message_code"] == "discovery_restore_required"
            assert restore_required["phase"] == "mode_restore"
        await _queue_shadow_update(hass, websocket, baseline)
        completed_cycles += 1
        mode_response = await advance_cycle(baseline)
        assert mode_response["message_code"] == "discovery_cycle_recorded"
        assert mode_response["completed_cycle_count"] == completed_cycles

    for target_level in (1, 2, 4, 5):
        for round_number in (1, 2):
            begin_response = await begin_cycle(
                "global_fan_level",
                round_number,
                source_level=3,
                target_level=target_level,
            )
            assert begin_response["phase"] == "carrier_on"
            for phase, state in (
                ("parameter_change", _shadow_state(ventilation=True)),
                (
                    "parameter_restore",
                    _shadow_state(ventilation=True, fan_level=target_level),
                ),
                ("carrier_off", _shadow_state(ventilation=True)),
            ):
                progress = await advance_cycle(state)
                assert progress["phase"] == phase
            completed_cycles += 1
            fan_response = await advance_cycle(baseline)
            assert fan_response["message_code"] == "discovery_cycle_recorded"
            assert fan_response["completed_cycle_count"] == completed_cycles

    for round_number in (1, 2):
        begin_response = await begin_cycle(
            "ai_target_temperature",
            round_number,
            source_temperature=35,
            target_temperature=36,
        )
        assert begin_response["phase"] == "carrier_on"
        for phase, state in (
            ("parameter_change", _shadow_state(ai_warmth=True)),
            (
                "parameter_restore",
                _shadow_state(ai_warmth=True, temperature=36),
            ),
            ("carrier_off", _shadow_state(ai_warmth=True)),
        ):
            progress = await advance_cycle(state)
            assert progress["phase"] == phase
        completed_cycles += 1
        temperature_response = await advance_cycle(baseline)
        assert temperature_response["message_code"] == "discovery_cycle_recorded"
        assert temperature_response["completed_cycle_count"] == completed_cycles

    assert completed_cycles == 14

    finish_response = await hass.services.async_call(
        DOMAIN,
        FINISH_DISCOVERY,
        base,
        blocking=True,
        return_response=True,
    )
    assert set(finish_response) == {
        "state",
        "message_code",
        "report_available",
        "confirmed_candidate_count",
        "ambiguous_count",
        "observed_unidentified_count",
        "not_observed_count",
        "invalid_count",
        "coverage_not_started_count",
        "coverage_partial_count",
        "coverage_complete_count",
    }
    assert finish_response["state"] == "idle"
    assert finish_response["message_code"] == "discovery_report_saved"
    assert finish_response["report_available"] is True
    assert finish_response["coverage_complete_count"] == 4
    assert finish_response["coverage_not_started_count"] == 6

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    discovery_diagnostics = diagnostics["state_discovery"]
    assert discovery_diagnostics["report_available"] is True
    report = discovery_diagnostics["report"]
    assert report["schema_version"] == 2
    assert report["statistics"]["completed_cycles"] == completed_cycles
    coverage = {row["experiment"]: row["status"] for row in report["coverage"]}
    assert coverage["idle_environment"] == "complete"
    assert coverage["night_light"] == "complete"
    assert coverage["global_fan_level"] == "complete"
    assert coverage["ai_target_temperature"] == "complete"
    assert any(
        candidate["path"] == "service/6/property/2"
        and candidate["classification"] == "confirmed_candidate"
        for candidate in report["candidates"]
    )
    serialized = json.dumps(report, sort_keys=True)
    assert "123456789" not in serialized
    assert entry.entry_id not in serialized
    assert "clientToken" not in serialized
    assert "$aws/things/" not in serialized
    assert "synthetic-private-raw-marker" not in serialized
    assert str(archive_root) not in serialized

    archive = report["raw_archive"]
    assert archive["enabled"] is True
    assert archive["status"] == "complete"
    archive_directory = archive_root / archive["session_id"]
    events_path = archive_directory / "events.jsonl"
    manifest_path = archive_directory / "manifest.json"
    events_bytes = events_path.read_bytes()
    lines = events_bytes.splitlines()
    assert len(lines) == archive["event_count"]
    assert len(events_bytes) == archive["file_bytes"]
    assert hashlib.sha256(events_bytes).hexdigest() == archive["sha256"]
    assert stat.S_IMODE(archive_directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(events_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(manifest_path.stat().st_mode) == 0o600
    assert any(
        b"synthetic-private-raw-marker" in base64.b64decode(json.loads(line)["payload_base64"])
        for line in lines
    )
    entities = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    light = next(entity for entity in entities if entity.domain == "light")
    light_state = hass.states.get(light.entity_id)
    assert light_state is not None
    assert light_state.state == "off"
    assert light_state.attributes["state_source"] == "get_reported"
    assert control_calls == []

    report_store = entry.runtime_data.discovery_store
    await _unload(hass, entry)
    assert await report_store.async_load() == report
    for service in (
        START_DISCOVERY,
        BEGIN_DISCOVERY_STEP,
        ADVANCE_DISCOVERY_STEP,
        FINISH_DISCOVERY,
        CANCEL_DISCOVERY,
    ):
        assert not hass.services.has_service(DOMAIN, service)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await _wait_until(lambda: entry.runtime_data.coordinator.discovery_available)
    assert len(session.calls) == 3
    reloaded_diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert reloaded_diagnostics["state_discovery"]["report"] == report
    assert len(tuple(archive_root.iterdir())) == 1

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    removed_store = DiscoveryReportStore(hass, entry.entry_id, lambda report: report)
    assert await removed_store.async_load() is None
    for service in (
        START_DISCOVERY,
        BEGIN_DISCOVERY_STEP,
        ADVANCE_DISCOVERY_STEP,
        FINISH_DISCOVERY,
        CANCEL_DISCOVERY,
    ):
        assert not hass.services.has_service(DOMAIN, service)
    assert events_path.exists()
    await asyncio.sleep(0)
    tasks_after = {
        task
        for task in asyncio.all_tasks()
        if task is not current
        and not task.done()
        and (
            task.get_name().startswith("aupu_q360_wss")
            or task.get_name().startswith("aupu_q360_discovery")
        )
    }
    assert tasks_after <= tasks_before


async def test_real_archive_mount_failure_precedes_discovery_network_and_control(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Catch an unavailable private mount sending a correlated get or device control."""
    initial_websocket = _FakeWebSocket()
    websocket = _FakeWebSocket()
    session = _FakeSession([initial_websocket, websocket])
    sleep = _ControlledSleep()
    original_init = AupuShadowWebSocket.__init__
    original_archive_open = RawDiscoveryArchive.async_open.__func__
    missing_root = tmp_path / "missing-private-archive"
    control_calls: list[bool] = []

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

    async def fake_set_light(self: AupuApiClient, is_on: bool) -> ApiResponse:
        del self
        control_calls.append(is_on)
        return ApiResponse(status=200, result={}, timestamp=0)

    async def open_missing_archive(
        cls: type[RawDiscoveryArchive],
        on_failure: Callable[[str], None],
        **kwargs: object,
    ) -> RawDiscoveryArchive:
        assert kwargs == {}
        return await original_archive_open(cls, on_failure, root=missing_root)

    monkeypatch.setattr(AupuShadowWebSocket, "__init__", init_with_fake_sleep)
    monkeypatch.setattr(AupuApiClient, "get_wss_credentials", fake_get_wss_credentials)
    monkeypatch.setattr(AupuApiClient, "set_light", fake_set_light)
    monkeypatch.setattr(
        RawDiscoveryArchive,
        "async_open",
        classmethod(open_missing_archive),
    )
    monkeypatch.setattr(
        "custom_components.aupu_q360.async_get_clientsession",
        lambda _: session,
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="AUPU Q360",
        unique_id="synthetic-missing-archive-entry",
        data=_entry_data(
            token=_jwt(expires_in=7 * 24 * 60 * 60, subject="synthetic-missing-archive"),
            use_wss=True,
            user_uuid="synthetic-user-uuid",
            raw_archive_enabled=True,
        ),
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await _wait_until(lambda: entry.runtime_data.coordinator.discovery_available)
    start_task = asyncio.create_task(
        hass.services.async_call(
            DOMAIN,
            START_DISCOVERY,
            {
                "config_entry_id": entry.entry_id,
                "all_modes_off_confirmed": True,
            },
            blocking=True,
            return_response=True,
        )
    )
    await _wait_until(lambda: len(session.calls) == 2)
    await _wait_until(lambda: len(websocket.sent) == 4)
    await sleep.release_next()
    await _wait_until(lambda: websocket.sent[-1] == b"\xc0\x00")
    websocket.queue_binary(b"\xd0\x00")

    with pytest.raises(ServiceValidationError) as raised:
        await start_task

    assert raised.value.translation_key == DiscoveryRawArchiveUnavailableError.error_code
    assert initial_websocket.close_calls == 1
    assert not any(
        json.loads(packet.payload).get("clientToken")
        for raw in websocket.sent
        if (packet := decode_packets(raw)[0]).packet_type.name == "PUBLISH"
    )
    assert entry.runtime_data.discovery_session.state.value == "idle"
    assert control_calls == []
    assert not missing_root.exists()
    await _unload(hass, entry)


async def test_real_services_remain_registered_until_final_entry_unloads(
    hass: HomeAssistant,
) -> None:
    """Catch one Config Entry unregistering discovery actions still owned by another."""
    first = MockConfigEntry(
        domain=DOMAIN,
        title="AUPU Q360 first",
        unique_id="synthetic-multi-entry-first",
        data=_entry_data(token=_jwt(expires_in=7 * 24 * 60 * 60, subject="synthetic-multi-first")),
    )
    second = MockConfigEntry(
        domain=DOMAIN,
        title="AUPU Q360 second",
        unique_id="synthetic-multi-entry-second",
        data=_entry_data(token=_jwt(expires_in=7 * 24 * 60 * 60, subject="synthetic-multi-second")),
    )
    first.add_to_hass(hass)
    second.add_to_hass(hass)
    assert await hass.config_entries.async_setup(first.entry_id)
    await hass.async_block_till_done()
    assert hasattr(second, "runtime_data")

    service_names = (
        START_DISCOVERY,
        BEGIN_DISCOVERY_STEP,
        ADVANCE_DISCOVERY_STEP,
        FINISH_DISCOVERY,
        CANCEL_DISCOVERY,
    )
    assert all(hass.services.has_service(DOMAIN, service) for service in service_names)

    await _unload(hass, first)
    assert all(hass.services.has_service(DOMAIN, service) for service in service_names)

    await _unload(hass, second)
    assert all(not hass.services.has_service(DOMAIN, service) for service in service_names)


async def test_real_https_only_discovery_fails_without_transport_or_control(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep discovery fail-closed when this Config Entry has no subscribed WSS."""
    control_calls: list[bool] = []

    async def fake_set_light(self: AupuApiClient, is_on: bool) -> ApiResponse:
        del self
        control_calls.append(is_on)
        return ApiResponse(status=200, result={}, timestamp=0)

    monkeypatch.setattr(AupuApiClient, "set_light", fake_set_light)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="AUPU Q360",
        unique_id="synthetic-https-discovery-entry",
        data=_entry_data(
            token=_jwt(expires_in=7 * 24 * 60 * 60, subject="synthetic-https-discovery")
        ),
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    with pytest.raises(ServiceValidationError) as raised:
        await hass.services.async_call(
            DOMAIN,
            START_DISCOVERY,
            {
                "config_entry_id": entry.entry_id,
                "all_modes_off_confirmed": True,
            },
            blocking=True,
            return_response=True,
        )

    assert raised.value.translation_key == "discovery_wss_unavailable"
    assert control_calls == []
    await _unload(hass, entry)


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
