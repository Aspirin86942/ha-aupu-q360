"""Guards proving v2 discovery has one read-only network boundary."""

from __future__ import annotations

import ast
import asyncio
import base64
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import aiohttp
import pytest
from homeassistant.core import HomeAssistant

from custom_components.aupu_q360.api import AupuApiClient, WssCredentials
from custom_components.aupu_q360.auth import BearerCredential
from custom_components.aupu_q360.coordinator import AupuCoordinator
from custom_components.aupu_q360.discovery import PanelStateDiscoverySession
from custom_components.aupu_q360.discovery_catalog import build_step_request
from custom_components.aupu_q360.discovery_models import JsonObject
from custom_components.aupu_q360.discovery_report_schema import validate_discovery_report
from custom_components.aupu_q360.discovery_sanitizer import DiscoverySanitizer
from custom_components.aupu_q360.models import ApiResponse, DeviceConfig
from custom_components.aupu_q360.mqtt_codec import PacketType, decode_packets, encode_publish
from custom_components.aupu_q360.shadow import AcceptedShadow
from custom_components.aupu_q360.wss import AupuShadowWebSocket

_DEVICE = DeviceConfig(did="123456789", tag="synthetic-network-boundary")
_GET = f"$aws/things/{_DEVICE.did}/shadow/get"
_GET_ACCEPTED = f"{_GET}/accepted"
_DISCOVERY_TOKEN = re.compile(r"disc-[0-9a-f]{32}")
_DISCOVERY_MODULES = (
    "discovery.py",
    "discovery_catalog.py",
    "discovery_analysis.py",
    "discovery_report_schema.py",
    "raw_discovery_archive.py",
    "services.py",
)
_FORBIDDEN_SYMBOLS = {
    "AupuApiClient",
    "CONTROL_PATH",
    "set_light",
    "async_set_light",
}
_FORBIDDEN_PUBLISH_LITERALS = ("/appapi/iot/control", "/shadow/update")


def test_discovery_modules_cannot_reference_control_network_surfaces(
    project_root: Path,
) -> None:
    """Catch discovery code gaining an HTTP/control import or update publish route."""
    component_root = project_root / "custom_components/aupu_q360"

    for filename in _DISCOVERY_MODULES:
        path = component_root / filename
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        referenced = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} | {
            node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
        }
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        string_literals = tuple(
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        )

        assert referenced.isdisjoint(_FORBIDDEN_SYMBOLS), filename
        assert imported.isdisjoint(_FORBIDDEN_SYMBOLS), filename
        assert all(
            fragment not in literal
            for fragment in _FORBIDDEN_PUBLISH_LITERALS
            for literal in string_literals
        ), filename


@dataclass(frozen=True, slots=True)
class _FakeMessage:
    type: aiohttp.WSMsgType
    data: bytes


class _FakeWebSocket:
    """Complete MQTT setup locally and retain every emitted frame."""

    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.messages: asyncio.Queue[_FakeMessage] = asyncio.Queue()
        self.close_calls = 0
        for packet in (
            b"\x20\x02\x00\x00",
            b"\x90\x03\x00\x01\x00",
            b"\x90\x03\x00\x02\x00",
        ):
            self.queue_binary(packet)

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(data)

    async def receive(self) -> _FakeMessage:
        return await self.messages.get()

    async def close(self) -> None:
        self.close_calls += 1

    def queue_binary(self, data: bytes) -> None:
        self.messages.put_nowait(_FakeMessage(aiohttp.WSMsgType.BINARY, data))


class _FakeSession:
    """Replace aiohttp only at the WSS connection boundary."""

    def __init__(self, websocket: _FakeWebSocket) -> None:
        self.websocket = websocket
        self.calls = 0

    async def ws_connect(
        self,
        url: str,
        *,
        params: dict[str, str],
        protocols: tuple[str, ...],
    ) -> _FakeWebSocket:
        del url, params, protocols
        self.calls += 1
        return self.websocket


class _FakeApi:
    """Supply WSS credentials while making any control request a test failure."""

    def __init__(self) -> None:
        self.credential_calls = 0
        self.control_calls: list[bool] = []

    async def get_wss_credentials(self) -> WssCredentials:
        self.credential_calls += 1
        return WssCredentials(
            authorizer_name="synthetic-authorizer",
            signature="synthetic-signature",
            token_key_name="synthetic-token-key",
        )

    async def set_light(self, is_on: bool) -> ApiResponse:
        self.control_calls.append(is_on)
        raise AssertionError("discovery attempted a control request")


class _ControlledSleep:
    """Keep WSS ping/retry tasks cancellable without wall-clock waits."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        await asyncio.Event().wait()


def _credential() -> BearerCredential:
    payload = json.dumps(
        {"exp": int((datetime.now(UTC) + timedelta(days=2)).timestamp())},
        separators=(",", ":"),
    ).encode()
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return BearerCredential.parse(f"e30.{encoded}.synthetic-tail")


def _reported_state(
    *,
    light: bool = False,
    night_light: bool = False,
    ventilation: bool = False,
    fan_level: int = 3,
    ai_warmth: bool = False,
    temperature: int = 35,
) -> dict[str, object]:
    return {
        "reported": {
            _DEVICE.did: {
                "2": {"properties": {"1": light}},
                "5": {"properties": {"1": night_light}},
                "6": {"properties": {"1": ventilation, "2": fan_level}},
                "7": {"properties": {"1": ai_warmth, "2": temperature}},
            }
        }
    }


async def _wait_until(predicate: Callable[[], bool]) -> None:
    for _ in range(200):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was not reached")


async def _snapshot_action[T](
    websocket: _FakeWebSocket,
    action: Awaitable[T],
    state: dict[str, object],
) -> T:
    sent_before = len(websocket.sent)
    task = asyncio.create_task(action)
    await _wait_until(lambda: len(websocket.sent) > sent_before)
    packet = decode_packets(websocket.sent[-1])[0]
    assert packet.packet_type is PacketType.PUBLISH
    assert packet.topic == _GET
    request = json.loads(packet.payload)
    token = request["clientToken"]
    assert isinstance(token, str)
    websocket.queue_binary(
        encode_publish(
            _GET_ACCEPTED,
            json.dumps(
                {"clientToken": token, "state": state},
                separators=(",", ":"),
            ).encode(),
        )
    )
    return await task


@pytest.mark.asyncio
async def test_complete_session_publishes_only_correlated_shadow_gets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch any phase using control HTTPS, a second connection, or a writable Shadow topic."""
    websocket = _FakeWebSocket()
    fake_session = _FakeSession(websocket)
    fake_api = _FakeApi()
    sleep = _ControlledSleep()
    original_init = AupuShadowWebSocket.__init__

    def init_with_controlled_sleep(
        client: AupuShadowWebSocket,
        *args: object,
        **kwargs: object,
    ) -> None:
        kwargs["sleep"] = sleep
        original_init(client, *args, **kwargs)

    monkeypatch.setattr(AupuShadowWebSocket, "__init__", init_with_controlled_sleep)
    coordinator = AupuCoordinator(
        hass=cast(HomeAssistant, type("_NoRepairHass", (), {"data": None})()),
        entry_id="synthetic-network-boundary-entry",
        credential=_credential(),
        api=cast(AupuApiClient, fake_api),
        async_request_reauth=lambda: None,
        session=cast(Any, fake_session),
        device=_DEVICE,
        use_wss=True,
        user_uuid="synthetic-user-uuid",
    )
    observer_remover: Callable[[], None] | None = None

    def activate_observer(
        observer: Callable[[AcceptedShadow], None],
        cancel: Callable[[], None],
    ) -> None:
        nonlocal observer_remover
        observer_remover = coordinator.async_set_discovery_observer(observer, cancel)

    def deactivate_observer() -> None:
        nonlocal observer_remover
        remove = observer_remover
        observer_remover = None
        if remove is not None:
            remove()

    reports: list[JsonObject] = []

    async def save_report(report: JsonObject) -> None:
        reports.append(report)

    session = PanelStateDiscoverySession(
        request_shadow_get=coordinator.async_request_shadow_get,
        save_report=save_report,
        sanitizer_factory=lambda key: DiscoverySanitizer(
            session_key=key,
            device_id=_DEVICE.did,
        ),
        validate_report=lambda report: validate_discovery_report(
            report,
            forbidden_values=(_DEVICE.did, _DEVICE.tag),
        ),
        activate_observer=activate_observer,
        deactivate_observer=deactivate_observer,
        discovery_available=lambda: coordinator.discovery_available,
        integration_version="0.2.0",
        now=lambda: datetime(2026, 9, 3, 0, 0, tzinfo=UTC),
    )
    baseline = _reported_state()

    try:
        await coordinator.async_start()
        await _wait_until(lambda: len(websocket.sent) == 4)
        assert fake_session.calls == 1

        await _snapshot_action(
            websocket,
            session.async_start(all_modes_off_confirmed=True),
            baseline,
        )

        await _snapshot_action(
            websocket,
            session.async_begin_step(
                build_step_request(experiment="idle_environment", round_number=1)
            ),
            baseline,
        )
        await _snapshot_action(websocket, session.async_advance_step(), baseline)

        await _snapshot_action(
            websocket,
            session.async_begin_step(build_step_request(experiment="night_light", round_number=1)),
            baseline,
        )
        await _snapshot_action(
            websocket,
            session.async_advance_step(),
            _reported_state(light=True, night_light=True),
        )
        await _snapshot_action(websocket, session.async_advance_step(), baseline)

        await _snapshot_action(
            websocket,
            session.async_begin_step(
                build_step_request(
                    experiment="global_fan_level",
                    round_number=1,
                    source_level=3,
                    target_level=5,
                )
            ),
            baseline,
        )
        for state in (
            _reported_state(ventilation=True),
            _reported_state(ventilation=True, fan_level=5),
            _reported_state(ventilation=True),
            baseline,
        ):
            await _snapshot_action(websocket, session.async_advance_step(), state)

        await _snapshot_action(
            websocket,
            session.async_begin_step(
                build_step_request(
                    experiment="ai_target_temperature",
                    round_number=1,
                    source_temperature=35,
                    target_temperature=36,
                )
            ),
            baseline,
        )
        for state in (
            _reported_state(ai_warmth=True),
            _reported_state(ai_warmth=True, temperature=36),
            _reported_state(ai_warmth=True),
            baseline,
        ):
            await _snapshot_action(websocket, session.async_advance_step(), state)

        report = await session.async_finish()
        assert report == reports[0]
        assert coordinator.is_on is False
    finally:
        await session.async_stop()
        await coordinator.async_stop()

    publishes = [
        packet
        for frame in websocket.sent
        for packet in decode_packets(frame)
        if packet.packet_type is PacketType.PUBLISH
    ]
    assert publishes[0].topic == _GET
    assert publishes[0].payload == b"{}"
    discovery_publishes = publishes[1:]
    assert discovery_publishes
    for packet in discovery_publishes:
        assert packet.topic == _GET
        assert packet.payload.count(b'"clientToken"') == 1
        payload = json.loads(packet.payload)
        assert set(payload) == {"clientToken"}
        assert _DISCOVERY_TOKEN.fullmatch(payload["clientToken"]) is not None
    assert fake_session.calls == 1
    assert fake_api.control_calls == []
    assert websocket.close_calls == 1
