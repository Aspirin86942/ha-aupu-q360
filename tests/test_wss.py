"""Deterministic lifecycle tests for the AWS IoT Shadow WSS transport."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Awaitable, Callable, Generator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import wraps
from typing import Any, cast

import aiohttp
import pytest

from custom_components.aupu_q360.api import AupuApiClient, WssCredentials
from custom_components.aupu_q360.auth import BearerCredential
from custom_components.aupu_q360.errors import AupuAuthError, AupuProtocolError
from custom_components.aupu_q360.models import DeviceConfig
from custom_components.aupu_q360.mqtt_codec import (
    PacketType,
    decode_packets,
    encode_publish,
)
from custom_components.aupu_q360.shadow import AcceptedShadow
from custom_components.aupu_q360.wss import _MAX_WSS_PACKET_BYTES, AupuShadowWebSocket

WSS_ENDPOINT = "wss://aii5h05kuofsj.ats.iot.cn-north-1.amazonaws.com.cn/mqtt"
DEVICE = DeviceConfig(did="123456789", tag="synthetic-tag")
UPDATE_ACCEPTED = "$aws/things/123456789/shadow/update/accepted"
_TEST_LOOP = asyncio.new_event_loop()


@pytest.fixture(scope="module", autouse=True)
def _close_test_loop() -> Generator[None]:
    yield
    _TEST_LOOP.close()


def direct_step(
    function: Callable[..., Awaitable[None]],
) -> Callable[..., None]:
    """Run async lifecycle scenarios on the pre-guard Windows event loop."""

    @wraps(function)
    def run(*args: Any, **kwargs: Any) -> None:
        _TEST_LOOP.run_until_complete(function(*args, **kwargs))

    return run


def _token() -> str:
    payload = json.dumps({"exp": int((datetime.now(UTC) + timedelta(days=2)).timestamp())}).encode()
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return f"e30.{encoded}.tailABCD"


class FakeApi:
    """Return synthetic one-use WSS credentials without any HTTPS request."""

    def __init__(self) -> None:
        self.calls = 0
        self.failure: Exception | None = None

    async def get_wss_credentials(self) -> WssCredentials:
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        suffix = str(self.calls)
        return WssCredentials(
            authorizer_name=f"synthetic-authorizer-{suffix}",
            signature=f"synthetic-signature-{suffix}",
            token_key_name=f"synthetic-token-key-{suffix}",
        )


@dataclass(frozen=True, slots=True)
class FakeMessage:
    type: aiohttp.WSMsgType
    data: bytes | str


class FakeWebSocket:
    """Faithfully model binary receive blocking, sends, and explicit close."""

    def __init__(
        self,
        *,
        auto_ping_response: bool = False,
        ping_failure: Exception | None = None,
        send_failure_at: int | None = None,
        send_failure: Exception | None = None,
    ) -> None:
        self.sent: list[bytes] = []
        self.messages: asyncio.Queue[FakeMessage] = asyncio.Queue()
        self.receive_started = asyncio.Event()
        self.auto_ping_response = auto_ping_response
        self.ping_failure = ping_failure
        self.send_failure_at = send_failure_at
        self.send_failure = send_failure
        self.closed = False
        self.close_calls = 0

    def queue_binary(self, data: bytes) -> None:
        self.messages.put_nowait(FakeMessage(aiohttp.WSMsgType.BINARY, data))

    def queue_text(self, data: str) -> None:
        self.messages.put_nowait(FakeMessage(aiohttp.WSMsgType.TEXT, data))

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(data)
        if len(self.sent) == self.send_failure_at and self.send_failure is not None:
            raise self.send_failure
        if data == b"\xc0\x00" and self.ping_failure is not None:
            raise self.ping_failure
        if data == b"\xc0\x00" and self.auto_ping_response:
            self.queue_binary(b"\xd0\x00")

    async def receive(self) -> FakeMessage:
        self.receive_started.set()
        return await self.messages.get()

    async def close(self) -> None:
        self.close_calls += 1
        self.closed = True


class FakeSession:
    """Record the exact aiohttp ws_connect boundary and return queued outcomes."""

    def __init__(self, outcomes: list[FakeWebSocket | BaseException]) -> None:
        self.outcomes = outcomes
        self.calls: list[tuple[str, dict[str, str], tuple[str, ...]]] = []

    async def ws_connect(
        self,
        url: str,
        *,
        params: dict[str, str],
        protocols: tuple[str, ...],
    ) -> FakeWebSocket:
        self.calls.append((url, dict(params), protocols))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class ControlledSleep:
    """Expose requested delays while keeping retry and ping waits cancellable."""

    def __init__(self) -> None:
        self.delays: list[float] = []
        self.waiters: asyncio.Queue[asyncio.Future[None]] = asyncio.Queue()

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        waiter = asyncio.get_running_loop().create_future()
        self.waiters.put_nowait(waiter)
        await waiter

    async def release_next(self) -> None:
        waiter = await self.waiters.get()
        if not waiter.done():
            waiter.set_result(None)
        await asyncio.sleep(0)


async def _wait_until(predicate: Callable[[], bool]) -> None:
    for _ in range(200):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was not reached")


def _ready_socket(*, auto_ping_response: bool = False) -> FakeWebSocket:
    websocket = FakeWebSocket(auto_ping_response=auto_ping_response)
    first_suback = b"\x90\x03\x00\x01\x00"
    websocket.queue_binary(b"\x20\x02\x00\x00" + first_suback[:2])
    websocket.queue_binary(first_suback[2:] + b"\x90\x03\x00\x02\x00")
    return websocket


def _client(
    *,
    api: FakeApi,
    session: FakeSession,
    sleep: Callable[[float], Awaitable[None]],
    user_uuid: str | None = "synthetic-user-uuid",
    connections: list[tuple[bool, bool]] | None = None,
    auth_failures: list[None] | None = None,
    updates: list[AcceptedShadow] | None = None,
) -> AupuShadowWebSocket:
    connection_events = [] if connections is None else connections
    auth_events = [] if auth_failures is None else auth_failures
    shadow_updates = [] if updates is None else updates

    def parse_shadow(topic: str, payload: bytes) -> AcceptedShadow | None:
        if topic != UPDATE_ACCEPTED or payload != b"synthetic-shadow":
            raise AupuProtocolError
        return AcceptedShadow(
            topic_kind="update",
            state={"reported": {DEVICE.did: {"2": {"properties": {"1": False}}}}},
        )

    return AupuShadowWebSocket(
        session=cast(aiohttp.ClientSession, session),
        api=cast(AupuApiClient, api),
        credential=BearerCredential.parse(_token()),
        device=DEVICE,
        user_uuid=user_uuid,
        async_connection_changed=lambda connected, healthy: connection_events.append(
            (connected, healthy)
        ),
        async_auth_failed=lambda: auth_events.append(None),
        parse_shadow=parse_shadow,
        async_shadow_message=shadow_updates.append,
        clock_ms=lambda: 1_700_000_000_123,
        sleep=sleep,
    )


@direct_step
async def test_connect_subscribe_get_ping_shadow_and_disconnect_in_order() -> None:
    """Catch wrong handshake order, topics, keepalive, or dropped reported state."""
    websocket = _ready_socket(auto_ping_response=True)
    session = FakeSession([websocket])
    api = FakeApi()
    sleep = ControlledSleep()
    connections: list[tuple[bool, bool]] = []
    updates: list[AcceptedShadow] = []
    client = _client(
        api=api,
        session=session,
        sleep=sleep,
        connections=connections,
        updates=updates,
    )

    await client.async_start()
    await _wait_until(lambda: len(websocket.sent) == 4)

    assert api.calls == 1
    assert session.calls == [
        (
            WSS_ENDPOINT,
            {
                "x-amz-customauthorizer-name": "synthetic-authorizer-1",
                "x-amz-customauthorizer-signature": "synthetic-signature-1",
                "tokenKeyName": "synthetic-token-key-1",
            },
            ("mqtt",),
        )
    ]
    packets = [decode_packets(raw)[0] for raw in websocket.sent]
    assert [packet.packet_type for packet in packets] == [
        PacketType.CONNECT,
        PacketType.SUBSCRIBE,
        PacketType.SUBSCRIBE,
        PacketType.PUBLISH,
    ]
    assert packets[0].topic == "synthetic-user-uuid-tailABCD-1700000000123"
    assert packets[0].keep_alive == 30
    assert packets[0].clean_session is True
    assert [(packet.packet_identifier, packet.topic) for packet in packets[1:3]] == [
        (1, "$aws/things/123456789/shadow/update/accepted"),
        (2, "$aws/things/123456789/shadow/get/accepted"),
    ]
    assert (packets[3].topic, packets[3].payload) == (
        "$aws/things/123456789/shadow/get",
        b"{}",
    )
    assert connections == [(True, False)]
    await _wait_until(lambda: sleep.delays == [30])
    assert sleep.delays == [30]

    websocket.queue_binary(encode_publish(UPDATE_ACCEPTED, b"synthetic-shadow"))
    await _wait_until(lambda: bool(updates))
    assert updates == [
        AcceptedShadow(
            topic_kind="update",
            state={"reported": {DEVICE.did: {"2": {"properties": {"1": False}}}}},
        )
    ]

    await sleep.release_next()
    await _wait_until(lambda: connections[-1:] == [(True, True)])
    assert websocket.sent[-1] == b"\xc0\x00"

    await client.async_stop()
    assert websocket.sent[-1] == b"\xe0\x00"
    assert websocket.close_calls == 1
    assert connections[-1] == (False, False)
    assert client.is_running is False


@direct_step
async def test_correlated_shadow_get_only_sends_on_the_current_ready_connection() -> None:
    """Catch discovery gets being queued, replayed, or emitted before subscription."""
    websocket = _ready_socket()
    sleep = ControlledSleep()
    client = _client(api=FakeApi(), session=FakeSession([websocket]), sleep=sleep)
    client_token = "disc-0123456789abcdef0123456789abcdef"

    with pytest.raises(AupuProtocolError):
        await client.async_request_shadow_get(client_token)

    await client.async_start()
    await _wait_until(lambda: len(websocket.sent) == 4)
    await client.async_request_shadow_get(client_token)

    packet = decode_packets(websocket.sent[-1])[0]
    assert packet.packet_type is PacketType.PUBLISH
    assert packet.topic == "$aws/things/123456789/shadow/get"
    assert json.loads(packet.payload) == {"clientToken": client_token}

    await client.async_stop()
    with pytest.raises(AupuProtocolError):
        await client.async_request_shadow_get(client_token)
    assert len(websocket.sent) == 6  # CONNECT, 2 SUBSCRIBE, 2 GET, DISCONNECT


@direct_step
async def test_correlated_shadow_get_rejects_uncontrolled_tokens() -> None:
    """Catch arbitrary strings entering a Shadow request or later report correlation."""
    client = _client(api=FakeApi(), session=FakeSession([]), sleep=ControlledSleep())

    for token in ("", "disc-not-hex", "x" * 129, "disc-" + "0" * 31):
        with pytest.raises(AupuProtocolError):
            await client.async_request_shadow_get(token)


@direct_step
async def test_retry_refetches_credentials_with_capped_backoff_and_stop_cancels_sleep() -> None:
    """Catch cached credentials, wrong retry delays, or an unload-blocking sleep."""
    failures = [aiohttp.ClientConnectionError("synthetic transport") for _ in range(7)]
    session = FakeSession(failures)
    api = FakeApi()
    sleep = ControlledSleep()
    client = _client(api=api, session=session, sleep=sleep)

    await client.async_start()
    for expected_calls in range(1, 7):
        await _wait_until(lambda expected_calls=expected_calls: len(sleep.delays) == expected_calls)
        if expected_calls < 6:
            await sleep.release_next()

    assert sleep.delays == [2, 4, 8, 16, 30, 30]
    assert api.calls == 6
    assert [call[1]["tokenKeyName"] for call in session.calls] == [
        f"synthetic-token-key-{index}" for index in range(1, 7)
    ]

    await client.async_stop()
    assert client.is_running is False


@direct_step
async def test_suback_success_resets_accumulated_backoff_before_get_send() -> None:
    """Catch a failed initial shadow/get retaining pre-handshake 30-second backoff."""
    websocket = _ready_socket()
    websocket.send_failure_at = 4
    websocket.send_failure = aiohttp.ClientConnectionError("synthetic get failure")
    failures = [aiohttp.ClientConnectionError("synthetic transport") for _ in range(5)]
    session = FakeSession([*failures, websocket])
    sleep = ControlledSleep()
    client = _client(api=FakeApi(), session=session, sleep=sleep)

    await client.async_start()
    for expected_calls in range(1, 6):
        await _wait_until(lambda expected_calls=expected_calls: len(sleep.delays) == expected_calls)
        await sleep.release_next()
    await _wait_until(lambda: len(sleep.delays) == 6)

    assert sleep.delays == [2, 4, 8, 16, 30, 2]
    assert websocket.sent[-1] == b"\xe0\x00"
    assert websocket.close_calls == 1
    await client.async_stop()


@direct_step
async def test_ping_send_failure_cancels_receive_and_reconnects() -> None:
    """Catch a failed keepalive leaving the receive loop blocked forever."""
    websocket = FakeWebSocket(ping_failure=aiohttp.ClientConnectionError("synthetic ping failure"))
    first_suback = b"\x90\x03\x00\x01\x00"
    websocket.queue_binary(b"\x20\x02\x00\x00" + first_suback)
    websocket.queue_binary(b"\x90\x03\x00\x02\x00")
    sleep = ControlledSleep()
    client = _client(
        api=FakeApi(),
        session=FakeSession([websocket]),
        sleep=sleep,
    )

    await client.async_start()
    await _wait_until(lambda: sleep.delays == [30])
    await sleep.release_next()
    await _wait_until(lambda: sleep.delays == [30, 2])

    assert websocket.sent[-2:] == [b"\xc0\x00", b"\xe0\x00"]
    assert websocket.close_calls == 1
    await client.async_stop()


@direct_step
async def test_missing_pingresp_closes_backs_off_and_reconnects() -> None:
    """Catch a writable half-open socket preventing WSS self-recovery forever."""
    silent = _ready_socket()
    replacement = _ready_socket()
    sleep = ControlledSleep()
    session = FakeSession([silent, replacement])
    client = _client(api=FakeApi(), session=session, sleep=sleep)

    await client.async_start()
    await _wait_until(lambda: sleep.delays == [30])
    await sleep.release_next()
    await _wait_until(lambda: sleep.delays == [30, 10])
    await sleep.release_next()
    await _wait_until(lambda: sleep.delays == [30, 10, 2])

    assert silent.sent[-2:] == [b"\xc0\x00", b"\xe0\x00"]
    assert silent.close_calls == 1

    await sleep.release_next()
    await _wait_until(lambda: len(session.calls) == 2)
    assert len(replacement.sent) == 4
    await client.async_stop()


@direct_step
async def test_oversized_declared_packet_closes_and_enters_reconnect_backoff() -> None:
    """Catch WSS omitting the runtime decoder limit or failing to clean up on violation."""
    websocket = _ready_socket()
    sleep = ControlledSleep()
    client = _client(api=FakeApi(), session=FakeSession([websocket]), sleep=sleep)

    await client.async_start()
    await _wait_until(lambda: sleep.delays == [30])
    websocket.queue_binary(b"\x30" + bytes((0x80, 0x80, 0x04)))
    await _wait_until(lambda: sleep.delays == [30, 2])

    assert _MAX_WSS_PACKET_BYTES == 64 * 1024
    assert websocket.sent[-1] == b"\xe0\x00"
    assert websocket.close_calls == 1
    await client.async_stop()


@direct_step
@pytest.mark.parametrize("status", [401, 403])
async def test_handshake_auth_failure_notifies_once_and_stops_retry(status: int) -> None:
    """Catch AWS authentication failures entering the reconnect loop."""
    error = aiohttp.WSServerHandshakeError(
        request_info=cast(Any, None),
        history=(),
        status=status,
        message="synthetic-secret-url",
        headers=None,
    )
    session = FakeSession([error])
    api = FakeApi()
    sleep = ControlledSleep()
    auth_failures: list[None] = []
    client = _client(
        api=api,
        session=session,
        sleep=sleep,
        auth_failures=auth_failures,
    )

    await client.async_start()
    await _wait_until(lambda: auth_failures == [None])

    assert api.calls == 1
    assert sleep.delays == []
    assert client.is_running is False


@direct_step
async def test_credential_auth_failure_notifies_without_opening_websocket() -> None:
    """Catch AUPU credential rejection being retried or reaching AWS."""
    api = FakeApi()
    api.failure = AupuAuthError()
    session = FakeSession([])
    auth_failures: list[None] = []
    client = _client(
        api=api,
        session=session,
        sleep=ControlledSleep(),
        auth_failures=auth_failures,
    )

    await client.async_start()
    await _wait_until(lambda: auth_failures == [None])

    assert api.calls == 1
    assert session.calls == []
    assert client.is_running is False


@direct_step
async def test_missing_user_uuid_performs_zero_network_work() -> None:
    """Catch an incomplete opt-in producing credentials or network side effects."""
    api = FakeApi()
    session = FakeSession([])
    client = _client(
        api=api,
        session=session,
        sleep=ControlledSleep(),
        user_uuid=None,
    )

    await client.async_start()

    assert api.calls == 0
    assert session.calls == []
    assert client.is_running is False
    await client.async_stop()


@direct_step
async def test_stop_cancels_receive_and_ping_without_background_task_leak() -> None:
    """Catch unload leaving a blocked receive or keepalive task behind."""
    websocket = _ready_socket()
    session = FakeSession([websocket])
    sleep = ControlledSleep()
    client = _client(api=FakeApi(), session=session, sleep=sleep)
    current = asyncio.current_task()
    before = {task for task in asyncio.all_tasks() if task is not current}

    await client.async_start()
    await websocket.receive_started.wait()
    await _wait_until(lambda: sleep.delays == [30])
    await client.async_stop()
    await asyncio.sleep(0)

    after = {task for task in asyncio.all_tasks() if task is not current and not task.done()}
    assert after <= before
    assert websocket.sent[-1] == b"\xe0\x00"
    assert websocket.close_calls == 1


@direct_step
async def test_external_cancellation_of_stop_is_not_swallowed() -> None:
    """Catch async_stop converting caller cancellation into apparent success."""

    class BlockingCloseWebSocket(FakeWebSocket):
        def __init__(self) -> None:
            super().__init__()
            self.close_started = asyncio.Event()
            self.close_gate = asyncio.Event()

        async def close(self) -> None:
            self.close_calls += 1
            self.close_started.set()
            await self.close_gate.wait()
            self.closed = True

    websocket = BlockingCloseWebSocket()
    first_suback = b"\x90\x03\x00\x01\x00"
    websocket.queue_binary(b"\x20\x02\x00\x00" + first_suback)
    websocket.queue_binary(b"\x90\x03\x00\x02\x00")
    client = _client(
        api=FakeApi(),
        session=FakeSession([websocket]),
        sleep=ControlledSleep(),
    )
    await client.async_start()
    await _wait_until(lambda: len(websocket.sent) == 4)

    stop_task = asyncio.create_task(client.async_stop())
    await websocket.close_started.wait()
    stop_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await stop_task
    assert client.is_running is False


@direct_step
async def test_auth_failure_does_not_log_url_token_or_query_values(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Catch diagnostics exposing the endpoint, JWT, or custom-authorizer values."""
    error = aiohttp.WSServerHandshakeError(
        request_info=cast(Any, None),
        history=(),
        status=401,
        message="synthetic-handshake-detail",
        headers=None,
    )
    api = FakeApi()
    session = FakeSession([error])
    failures: list[None] = []
    client = _client(
        api=api,
        session=session,
        sleep=ControlledSleep(),
        auth_failures=failures,
    )

    await client.async_start()
    await _wait_until(lambda: failures == [None])

    rendered = caplog.text
    forbidden = (
        WSS_ENDPOINT,
        _token(),
        "synthetic-authorizer-1",
        "synthetic-signature-1",
        "synthetic-token-key-1",
        "synthetic-handshake-detail",
    )
    assert all(value not in rendered for value in forbidden)


@direct_step
async def test_unclassified_runner_failure_emits_one_fixed_redacted_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Catch programming errors silently terminating the WSS state channel."""
    forbidden = (
        WSS_ENDPOINT,
        _token(),
        "synthetic-authorizer-1",
        "synthetic-signature-1",
        "synthetic-token-key-1",
        "synthetic-user-uuid-tailABCD-1700000000123",
        DEVICE.did,
    )
    api = FakeApi()
    api.failure = RuntimeError("|".join(forbidden))
    client = _client(
        api=api,
        session=FakeSession([]),
        sleep=ControlledSleep(),
    )

    await client.async_start()
    await _wait_until(lambda: not client.is_running)
    await _wait_until(
        lambda: any(record.name == "custom_components.aupu_q360.wss" for record in caplog.records)
    )

    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "custom_components.aupu_q360.wss"
    ]
    assert messages == ["AUPU WSS runner stopped unexpectedly"]
    assert all(value not in caplog.text for value in forbidden)


def test_wss_credentials_repr_and_protocol_errors_are_secret_free() -> None:
    """Catch WSS credential values entering repr or malformed-response errors."""
    credentials = WssCredentials(
        authorizer_name="synthetic-authorizer-secret",
        signature="synthetic-signature-secret",
        token_key_name="synthetic-token-secret",
    )

    rendered = repr(credentials)
    assert "synthetic-authorizer-secret" not in rendered
    assert "synthetic-signature-secret" not in rendered
    assert "synthetic-token-secret" not in rendered
