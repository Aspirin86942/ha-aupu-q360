"""Contract tests for the one-shot AUPU HTTPS control client."""

from __future__ import annotations

import asyncio
import traceback
from collections.abc import Coroutine, Generator
from typing import Any, Self, cast

import aiohttp
import pytest

from custom_components.aupu_q360.api import (
    AupuApiClient,
    PhoneLoginResult,
    build_light_control_body,
)
from custom_components.aupu_q360.auth import BearerCredential
from custom_components.aupu_q360.errors import (
    AupuAuthError,
    AupuProtocolError,
    AupuRateLimitError,
    AupuTemporaryError,
)
from custom_components.aupu_q360.models import DeviceConfig
from custom_components.aupu_q360.signer import AppAuthorizationSigner

_TEST_LOOP = asyncio.new_event_loop()


@pytest.fixture(scope="module", autouse=True)
def _close_test_loop() -> Generator[None]:
    yield
    _TEST_LOOP.close()


def _run[T](awaitable: Coroutine[Any, Any, T]) -> T:
    return _TEST_LOOP.run_until_complete(awaitable)


def _json_diff(left: object, right: object, path: str = "") -> set[str]:
    if isinstance(left, dict) and isinstance(right, dict) and left.keys() == right.keys():
        differences: set[str] = set()
        for key in left:
            child_path = f"{path}.{key}" if path else str(key)
            differences.update(_json_diff(left[key], right[key], child_path))
        return differences
    return set() if left == right else {path}


class _Signer:
    def __init__(self) -> None:
        self.call_count = 0

    def sign(self, timestamp: int | None = None) -> str:
        assert timestamp is None
        self.call_count += 1
        return f"dynamic-{self.call_count}"


class _Response:
    def __init__(
        self,
        status: int,
        payload: object = None,
        *,
        json_error: Exception | None = None,
        enter_error: Exception | None = None,
    ) -> None:
        self.status = status
        self._payload = payload
        self._json_error = json_error
        self._enter_error = enter_error

    async def __aenter__(self) -> Self:
        if self._enter_error is not None:
            raise self._enter_error
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def json(self) -> object:
        if self._json_error is not None:
            raise self._json_error
        return self._payload


class _Session:
    def __init__(self, responses: list[_Response]) -> None:
        self._responses = responses
        self.calls: list[dict[str, Any]] = []

    def request(self, method: str, url: str, **kwargs: object) -> _Response:
        self.calls.append({"method": method, "url": url, **kwargs})
        return self._responses.pop(0)


@pytest.fixture
def device() -> DeviceConfig:
    return DeviceConfig(did="123456789", tag="bedroom-light")


@pytest.fixture
def credential() -> BearerCredential:
    return BearerCredential.parse("e30.eyJleHAiOjQxMDI0NDQ4MDB9.signature")


def _client(
    session: _Session,
    signer: _Signer,
    credential: BearerCredential,
    device: DeviceConfig,
) -> AupuApiClient:
    return AupuApiClient(
        session=cast(aiohttp.ClientSession, session),
        signer=cast(AppAuthorizationSigner, signer),
        credential=credential,
        device=device,
    )


def test_light_bodies_only_differ_at_confirmed_boolean(device: DeviceConfig) -> None:
    on_body = build_light_control_body(device, is_on=True)
    off_body = build_light_control_body(device, is_on=False)

    assert set(on_body) == {"did", "tag", "topicName", "sendBody"}
    assert on_body["did"] == 123456789
    assert on_body["tag"] == "bedroom-light"
    assert on_body["topicName"] == "$aws/things/123456789/shadow/update"
    assert on_body["sendBody"]["state"]["desired"][device.did]["2"]["properties"]["1"] is True
    assert off_body["sendBody"]["state"]["desired"][device.did]["2"]["properties"]["1"] is False
    assert _json_diff(on_body, off_body) == {
        f"sendBody.state.desired.{device.did}.2.properties.1"
    }


@pytest.mark.parametrize("did", ["", " ", "12a", "-1", "1.0"])
def test_device_rejects_non_decimal_did(did: str) -> None:
    with pytest.raises(ValueError, match="Device identifier must be decimal digits"):
        DeviceConfig(did=did, tag="valid-tag")


@pytest.mark.parametrize("tag", ["", "   "])
def test_device_rejects_blank_tag(tag: str) -> None:
    with pytest.raises(ValueError, match="Device tag must be a non-empty string"):
        DeviceConfig(did="123", tag=tag)


def test_control_uses_fresh_signature_bearer_and_fixed_post_contract(
    device: DeviceConfig, credential: BearerCredential
) -> None:
    session = _Session(
        [
            _Response(200, {"status": 0, "result": {"accepted": True}, "timestamp": 100}),
            _Response(200, {"status": 0, "result": {"accepted": True}, "timestamp": 101}),
        ]
    )
    signer = _Signer()
    client = _client(session, signer, credential, device)

    first = _run(client.set_light(True))
    second = _run(client.set_light(False))

    assert (first.status, first.result, first.timestamp) == (0, {"accepted": True}, 100)
    assert (second.status, second.result, second.timestamp) == (0, {"accepted": True}, 101)
    assert signer.call_count == 2
    assert [call["method"] for call in session.calls] == ["POST", "POST"]
    assert [call["url"] for call in session.calls] == [
        "https://cn-north-1-prod.aupu.net/appapi/iot/control",
        "https://cn-north-1-prod.aupu.net/appapi/iot/control",
    ]
    assert [call["headers"] for call in session.calls] == [
        {"App-Authorization": "dynamic-1", "Authorization": credential.authorization_header},
        {"App-Authorization": "dynamic-2", "Authorization": credential.authorization_header},
    ]
    assert [call["allow_redirects"] for call in session.calls] == [False, False]
    assert session.calls[0]["json"] == build_light_control_body(device, is_on=True)
    assert session.calls[1]["json"] == build_light_control_body(device, is_on=False)


@pytest.mark.parametrize("redirect_status", [301, 302, 307, 308])
def test_redirect_is_protocol_error_without_following_or_replay(
    device: DeviceConfig,
    credential: BearerCredential,
    redirect_status: int,
) -> None:
    session = _Session([_Response(redirect_status)])

    with pytest.raises(AupuProtocolError, match="Service response is invalid"):
        _run(_client(session, _Signer(), credential, device).set_light(True))

    assert len(session.calls) == 1
    assert session.calls[0]["allow_redirects"] is False


@pytest.mark.parametrize(
    ("http_status", "payload", "expected_error"),
    [
        (401, None, AupuAuthError),
        (429, None, AupuRateLimitError),
        (500, None, AupuTemporaryError),
        (503, None, AupuTemporaryError),
        (400, None, AupuProtocolError),
        (200, {"status": 401, "result": None, "timestamp": 1}, AupuAuthError),
        (200, {"status": 1017, "result": None, "timestamp": 1}, AupuAuthError),
        (200, {"status": 1018, "result": None, "timestamp": 1}, AupuAuthError),
        (200, {"status": 77, "result": None, "timestamp": 1}, AupuProtocolError),
    ],
)
def test_response_failures_are_classified(
    device: DeviceConfig,
    credential: BearerCredential,
    http_status: int,
    payload: object,
    expected_error: type[Exception],
) -> None:
    session = _Session([_Response(http_status, payload)])
    client = _client(session, _Signer(), credential, device)

    with pytest.raises(expected_error):
        _run(client.set_light(True))

    assert len(session.calls) == 1


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"status": True, "result": None, "timestamp": 1},
        {"status": 0, "result": None},
        {"status": 0, "result": None, "timestamp": "invalid"},
    ],
)
def test_malformed_success_response_is_protocol_error(
    device: DeviceConfig, credential: BearerCredential, payload: object
) -> None:
    session = _Session([_Response(200, payload)])

    with pytest.raises(AupuProtocolError):
        _run(_client(session, _Signer(), credential, device).set_light(True))

    assert len(session.calls) == 1


def test_invalid_json_is_protocol_error_without_replay(
    device: DeviceConfig, credential: BearerCredential
) -> None:
    session = _Session([_Response(200, json_error=ValueError("private response"))])

    with pytest.raises(AupuProtocolError, match="Service response is invalid"):
        _run(_client(session, _Signer(), credential, device).set_light(True))

    assert len(session.calls) == 1


def test_damaged_payload_is_redacted_protocol_error_without_replay(
    device: DeviceConfig, credential: BearerCredential
) -> None:
    session = _Session(
        [_Response(200, json_error=aiohttp.ClientPayloadError("private compressed payload"))]
    )

    with pytest.raises(AupuProtocolError, match="Service response is invalid") as exc_info:
        _run(_client(session, _Signer(), credential, device).set_light(True))

    rendered_exception = "".join(traceback.format_exception(exc_info.value))
    assert "private compressed payload" not in rendered_exception
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__suppress_context__ is True
    assert len(session.calls) == 1


@pytest.mark.parametrize(
    "transport_error",
    [TimeoutError("private timeout"), aiohttp.ClientConnectionError("private connection")],
)
def test_transport_failure_is_temporary_without_replay(
    device: DeviceConfig,
    credential: BearerCredential,
    transport_error: Exception,
) -> None:
    session = _Session([_Response(200, enter_error=transport_error)])

    with pytest.raises(AupuTemporaryError, match="Temporary service failure"):
        _run(_client(session, _Signer(), credential, device).set_light(True))

    assert len(session.calls) == 1


def test_error_messages_do_not_echo_untrusted_transport_details(
    device: DeviceConfig, credential: BearerCredential
) -> None:
    del device, credential
    assert "private" not in str(AupuTemporaryError())
    assert "private" not in str(AupuProtocolError())


def test_sms_request_and_phone_login_use_fresh_signatures_without_bearer(
    device: DeviceConfig, credential: BearerCredential
) -> None:
    """Catch credential replay or a changed SMS endpoint/payload contract."""
    session = _Session(
        [
            _Response(200, {"status": 0, "result": None, "timestamp": 10}),
            _Response(
                200,
                {
                    "status": 0,
                    "result": {
                        "token": "synthetic.new.token",
                        "user": {"userUuid": "synthetic-user-uuid"},
                    },
                    "timestamp": 11,
                },
            ),
        ]
    )
    signer = _Signer()
    client = _client(session, signer, credential, device)

    _run(client.request_sms_code(phone="13800000000"))
    login = _run(client.login_by_phone(phone="13800000000", code="123456"))

    assert login == PhoneLoginResult(
        token="synthetic.new.token", user_uuid="synthetic-user-uuid"
    )
    assert signer.call_count == 2
    assert session.calls[0]["method"] == "GET"
    assert session.calls[0]["url"].endswith("/authserver/auth/user/terminal/smscode")
    assert session.calls[0]["params"] == {
        "areaCode": 86,
        "appKey": "AP",
        "phoneNum": "13800000000",
        "type": "LOG",
    }
    assert session.calls[0]["headers"] == {"App-Authorization": "dynamic-1"}
    assert session.calls[0]["json"] is None
    assert session.calls[1]["method"] == "POST"
    assert session.calls[1]["url"].endswith("/authserver/auth/user/terminal/loginByPhone")
    assert session.calls[1]["json"] == {
        "areaCode": 86,
        "appKey": "AP",
        "phone": "13800000000",
        "randomCode": "123456",
    }
    assert session.calls[1]["headers"] == {"App-Authorization": "dynamic-2"}


@pytest.mark.parametrize(
    "result",
    [
        None,
        {},
        {"token": "", "user": {"userUuid": "synthetic-user-uuid"}},
        {"token": "synthetic.new.token", "user": {}},
    ],
)
def test_phone_login_rejects_incomplete_response_without_echoing_it(
    device: DeviceConfig, credential: BearerCredential, result: object
) -> None:
    """Catch malformed authentication results becoming entry-ready credentials."""
    session = _Session([_Response(200, {"status": 0, "result": result, "timestamp": 10})])

    with pytest.raises(AupuProtocolError) as raised:
        _run(_client(session, _Signer(), credential, device).login_by_phone("13800000000", "123456"))

    assert "synthetic.new.token" not in str(raised.value)
