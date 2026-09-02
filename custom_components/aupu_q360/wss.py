"""Ephemeral AWS IoT MQTT-over-WebSocket lifecycle for one Q360 Shadow."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable

import aiohttp

from .api import AupuApiClient
from .auth import BearerCredential
from .errors import AupuAuthError, AupuError, AupuProtocolError
from .models import DeviceConfig
from .mqtt_codec import (
    MqttPacket,
    MqttPacketDecoder,
    PacketType,
    encode_connect,
    encode_disconnect,
    encode_pingreq,
    encode_publish,
    encode_subscribe,
)
from .shadow import AcceptedShadow, RawShadowEvent

_WSS_ENDPOINT = "wss://aii5h05kuofsj.ats.iot.cn-north-1.amazonaws.com.cn/mqtt"
_RETRY_DELAYS = (2.0, 4.0, 8.0, 16.0, 30.0)
_KEEP_ALIVE_SECONDS = 30
_PINGRESP_TIMEOUT_SECONDS = 10
_MAX_WSS_PACKET_BYTES = 64 * 1024
_DISCOVERY_TOKEN = re.compile(r"disc-[0-9a-f]{32}")
_LOGGER = logging.getLogger(__name__)

ConnectionCallback = Callable[[bool, bool], None]
ShadowParser = Callable[[str, bytes], AcceptedShadow | None]
OutgoingRecorder = Callable[[RawShadowEvent], None]


class AupuShadowWebSocket:
    """Own one cancellable WSS runner without retaining per-connection secrets."""

    def __init__(
        self,
        *,
        session: aiohttp.ClientSession,
        api: AupuApiClient,
        credential: BearerCredential,
        device: DeviceConfig,
        user_uuid: str | None,
        async_connection_changed: ConnectionCallback,
        async_auth_failed: Callable[[], None],
        parse_shadow: ShadowParser,
        async_shadow_message: Callable[[AcceptedShadow], None],
        clock_ms: Callable[[], int] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._session = session
        self._api = api
        self._credential = credential
        self._device = device
        self._user_uuid = user_uuid
        self._async_connection_changed = async_connection_changed
        self._async_auth_failed = async_auth_failed
        self._parse_shadow = parse_shadow
        self._async_shadow_message = async_shadow_message
        self._clock_ms = clock_ms or _unix_milliseconds
        self._sleep = sleep
        self._runner_task: asyncio.Task[None] | None = None
        self._ready_in_attempt = False
        self._active_websocket: aiohttp.ClientWebSocketResponse | None = None
        self._send_lock = asyncio.Lock()

    @property
    def is_running(self) -> bool:
        """Return whether the sole background runner is still active."""
        return self._runner_task is not None and not self._runner_task.done()

    async def async_start(self) -> None:
        """Start at most one runner; incomplete WSS opt-in performs no network work."""
        if self._user_uuid is None or self.is_running:
            return
        task = asyncio.create_task(self._run(), name="aupu_q360_wss")
        self._runner_task = task
        task.add_done_callback(self._runner_done)

    async def async_stop(self) -> None:
        """Cancel and await all WSS-owned work while preserving caller cancellation."""
        task = self._runner_task
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling() > 0:
                raise
        finally:
            if self._runner_task is task:
                self._runner_task = None

    async def async_request_shadow_get(
        self,
        client_token: str,
        record_outgoing: OutgoingRecorder | None = None,
    ) -> None:
        """Send one correlated Shadow get only on the current ready connection."""
        if not isinstance(client_token, str) or _DISCOVERY_TOKEN.fullmatch(client_token) is None:
            raise AupuProtocolError
        websocket = self._active_websocket
        if websocket is None:
            raise AupuProtocolError
        payload = json.dumps({"clientToken": client_token}, separators=(",", ":")).encode("utf-8")
        event = RawShadowEvent(
            direction="outgoing",
            topic=f"$aws/things/{self._device.did}/shadow/get",
            payload=payload,
        )
        async with self._send_lock:
            if websocket is not self._active_websocket:
                raise AupuProtocolError
            if record_outgoing is not None:
                record_outgoing(event)
            await websocket.send_bytes(encode_publish(event.topic, event.payload))

    def _runner_done(self, task: asyncio.Task[None]) -> None:
        """Release the completed task reference and consume unexpected task errors."""
        if not task.cancelled():
            exception = task.exception()
            if exception is not None:
                _LOGGER.error("AUPU WSS runner stopped unexpectedly")
        if self._runner_task is task:
            self._runner_task = None

    async def _run(self) -> None:
        retry_index = 0
        while True:
            self._ready_in_attempt = False
            try:
                await self._connect_once()
            except asyncio.CancelledError:
                raise
            except AupuAuthError:
                self._async_auth_failed()
                return
            except aiohttp.WSServerHandshakeError as exc:
                if exc.status in (401, 403):
                    self._async_auth_failed()
                    return
            except (AupuError, aiohttp.ClientError, TimeoutError):
                pass

            if self._ready_in_attempt:
                retry_index = 0
            delay = _RETRY_DELAYS[min(retry_index, len(_RETRY_DELAYS) - 1)]
            retry_index += 1
            await self._sleep(delay)

    async def _connect_once(self) -> None:
        credentials = await self._api.get_wss_credentials()
        raw_token = self._credential.authorization_header.removeprefix("Bearer ")
        client_id = f"{self._user_uuid}-{raw_token[-8:]}-{self._clock_ms()}"
        connect_packet = encode_connect(client_id, keep_alive=_KEEP_ALIVE_SECONDS)
        params = {
            "x-amz-customauthorizer-name": credentials.authorizer_name,
            "x-amz-customauthorizer-signature": credentials.signature,
            "tokenKeyName": credentials.token_key_name,
        }
        try:
            websocket = await self._session.ws_connect(
                _WSS_ENDPOINT,
                params=params,
                protocols=("mqtt",),
            )
        finally:
            del params, credentials, raw_token, client_id

        decoder = MqttPacketDecoder(max_packet_size=_MAX_WSS_PACKET_BYTES)
        pending: deque[MqttPacket] = deque()
        ping = _PingTracker()
        mqtt_connected = False
        ping_task: asyncio.Task[None] | None = None
        receive_task: asyncio.Task[None] | None = None
        try:
            await websocket.send_bytes(connect_packet)
            connack = await _receive_packet(websocket, decoder, pending)
            if connack.packet_type is not PacketType.CONNACK or connack.return_code != 0:
                raise AupuProtocolError
            mqtt_connected = True

            topics = (
                f"$aws/things/{self._device.did}/shadow/update/accepted",
                f"$aws/things/{self._device.did}/shadow/get/accepted",
            )
            for packet_identifier, topic in enumerate(topics, start=1):
                await websocket.send_bytes(encode_subscribe(packet_identifier, topic))

            expected_subacks = {1, 2}
            while expected_subacks:
                suback = await _receive_packet(websocket, decoder, pending)
                if (
                    suback.packet_type is not PacketType.SUBACK
                    or suback.packet_identifier not in expected_subacks
                    or suback.granted_qos != 0
                ):
                    raise AupuProtocolError
                expected_subacks.remove(suback.packet_identifier)

            self._ready_in_attempt = True
            await websocket.send_bytes(
                encode_publish(
                    f"$aws/things/{self._device.did}/shadow/get",
                    b"{}",
                )
            )
            self._active_websocket = websocket
            self._async_connection_changed(True, False)
            ping_task = asyncio.create_task(
                self._ping_loop(websocket, ping), name="aupu_q360_wss_ping"
            )
            receive_task = asyncio.create_task(
                self._receive_loop(websocket, decoder, pending, ping),
                name="aupu_q360_wss_receive",
            )
            session_tasks = {ping_task, receive_task}
            done, unfinished = await asyncio.wait(
                session_tasks, return_when=asyncio.FIRST_COMPLETED
            )
            for task in unfinished:
                task.cancel()
            await asyncio.gather(*unfinished, return_exceptions=True)
            for task in done:
                await task
        finally:
            if self._active_websocket is websocket:
                self._active_websocket = None
            background = tuple(task for task in (ping_task, receive_task) if task is not None)
            for task in background:
                task.cancel()
            if background:
                await asyncio.gather(*background, return_exceptions=True)
            if mqtt_connected:
                try:
                    await websocket.send_bytes(encode_disconnect())
                except (aiohttp.ClientError, RuntimeError):
                    pass
            try:
                await websocket.close()
            except (aiohttp.ClientError, RuntimeError):
                pass
            self._async_connection_changed(False, False)

    async def _ping_loop(
        self,
        websocket: aiohttp.ClientWebSocketResponse,
        ping: _PingTracker,
    ) -> None:
        while True:
            await self._sleep(_KEEP_ALIVE_SECONDS)
            ping.start()
            self._async_connection_changed(True, False)
            async with self._send_lock:
                await websocket.send_bytes(encode_pingreq())
            await self._wait_for_pingresp(ping)

    async def _wait_for_pingresp(self, ping: _PingTracker) -> None:
        """Fail the session when an outstanding PINGREQ misses its fixed deadline."""
        response_task = asyncio.create_task(_await_event(ping.response))
        deadline_task = asyncio.create_task(_await_sleep(self._sleep, _PINGRESP_TIMEOUT_SECONDS))
        try:
            done, _ = await asyncio.wait(
                {response_task, deadline_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if response_task in done:
                return
            ping.cancel()
            raise AupuProtocolError
        finally:
            for task in (response_task, deadline_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(response_task, deadline_task, return_exceptions=True)

    async def _receive_loop(
        self,
        websocket: aiohttp.ClientWebSocketResponse,
        decoder: MqttPacketDecoder,
        pending: deque[MqttPacket],
        ping: _PingTracker,
    ) -> None:
        while True:
            packet = await _receive_packet(websocket, decoder, pending)
            if packet.packet_type is PacketType.PINGRESP:
                if not ping.complete():
                    raise AupuProtocolError
                self._async_connection_changed(True, True)
                continue
            if packet.packet_type is not PacketType.PUBLISH or packet.topic is None:
                raise AupuProtocolError
            message = self._parse_shadow(packet.topic, packet.payload)
            if message is not None:
                self._async_shadow_message(message)


async def _receive_packet(
    websocket: aiohttp.ClientWebSocketResponse,
    decoder: MqttPacketDecoder,
    pending: deque[MqttPacket],
) -> MqttPacket:
    """Read binary frames until one complete MQTT packet is available."""
    while not pending:
        message = await websocket.receive()
        if message.type is not aiohttp.WSMsgType.BINARY or not isinstance(message.data, bytes):
            raise AupuProtocolError
        pending.extend(decoder.feed(message.data))
    return pending.popleft()


class _PingTracker:
    """Track the sole outstanding MQTT PINGREQ without retaining peer data."""

    def __init__(self) -> None:
        self.response = asyncio.Event()
        self.outstanding = False

    def start(self) -> None:
        self.response.clear()
        self.outstanding = True

    def complete(self) -> bool:
        if not self.outstanding:
            return False
        self.outstanding = False
        self.response.set()
        return True

    def cancel(self) -> None:
        self.outstanding = False
        self.response.clear()


async def _await_event(event: asyncio.Event) -> None:
    """Wait for an event while giving session task races one uniform result type."""
    await event.wait()


async def _await_sleep(sleep: Callable[[float], Awaitable[None]], delay: float) -> None:
    """Await an injected sleep as a concrete coroutine owned by this session."""
    await sleep(delay)


def _unix_milliseconds() -> int:
    """Return current Unix milliseconds for the per-connection client identifier."""
    return int(time.time() * 1000)
