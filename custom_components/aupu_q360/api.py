"""One-shot HTTPS control transport for the AUPU Q360 integration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import aiohttp

from .auth import BearerCredential
from .const import AUPU_BASE_URL, CONTROL_PATH, PHONE_LOGIN_PATH, SMS_CODE_PATH
from .errors import (
    AupuAuthError,
    AupuProtocolError,
    AupuRateLimitError,
    AupuTemporaryError,
)
from .models import ApiResponse, DeviceConfig
from .signer import AppAuthorizationSigner

_AUTH_STATUSES = frozenset({401, 1017, 1018})


@dataclass(frozen=True, slots=True, repr=False)
class PhoneLoginResult:
    """Validated credentials issued by a phone-code login response."""

    token: str
    user_uuid: str


def build_light_control_body(device: DeviceConfig, *, is_on: bool) -> dict[str, Any]:
    """Build the confirmed shadow update body for the Q360 light switch."""
    return {
        "did": int(device.did),
        "tag": device.tag,
        "topicName": device.topic_name,
        "sendBody": {
            "state": {
                "desired": {
                    device.did: {
                        "2": {
                            "properties": {
                                "1": is_on,
                            }
                        }
                    }
                }
            }
        },
    }


class AupuApiClient:
    """Send authenticated AUPU requests without automatic retries or replay."""

    def __init__(
        self,
        *,
        session: aiohttp.ClientSession,
        signer: AppAuthorizationSigner,
        credential: BearerCredential,
        device: DeviceConfig,
    ) -> None:
        self._session = session
        self._signer = signer
        self._credential = credential
        self._device = device

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, Any],
    ) -> ApiResponse:
        """Send one request and classify its HTTP, transport, and business result."""
        return await self._request(method, path, json=json, include_bearer=True)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, Any] | None,
        params: Mapping[str, Any] | None = None,
        include_bearer: bool,
    ) -> ApiResponse:
        """Send one signed request while keeping credentials out of unauthenticated calls."""
        headers = {
            "App-Authorization": self._signer.sign(),
        }
        if include_bearer:
            headers["Authorization"] = self._credential.authorization_header
        try:
            async with self._session.request(
                method,
                f"{AUPU_BASE_URL}{path}",
                headers=headers,
                json=None if json is None else dict(json),
                params=None if params is None else dict(params),
                allow_redirects=False,
            ) as response:
                _raise_for_http_status(response.status)
                try:
                    payload = await response.json()
                except (
                    aiohttp.ClientPayloadError,
                    aiohttp.ContentTypeError,
                    TypeError,
                    ValueError,
                ):
                    raise AupuProtocolError() from None
        except (TimeoutError, aiohttp.ClientConnectionError):
            raise AupuTemporaryError() from None

        return _parse_response(payload)

    async def request_sms_code(self, *, phone: str) -> None:
        """Request one login code without replaying the stored Bearer token."""
        await self._request(
            "GET",
            SMS_CODE_PATH,
            json=None,
            params={"areaCode": 86, "appKey": "AP", "phoneNum": phone, "type": "LOG"},
            include_bearer=False,
        )

    async def login_by_phone(self, phone: str, code: str) -> PhoneLoginResult:
        """Exchange one locally held code for validated replacement credentials."""
        response = await self._request(
            "POST",
            PHONE_LOGIN_PATH,
            json={"areaCode": 86, "appKey": "AP", "phone": phone, "randomCode": code},
            include_bearer=False,
        )
        return _parse_phone_login(response.result)

    async def set_light(self, is_on: bool) -> ApiResponse:
        """Apply one light state change with exactly one HTTPS request."""
        return await self.request(
            "POST",
            CONTROL_PATH,
            json=build_light_control_body(self._device, is_on=is_on),
        )


def _raise_for_http_status(status: int) -> None:
    """Classify HTTP status without incorporating remote response details."""
    if status == 401:
        raise AupuAuthError()
    if status == 429:
        raise AupuRateLimitError()
    if 500 <= status <= 599:
        raise AupuTemporaryError()
    if not 200 <= status <= 299:
        raise AupuProtocolError()


def _parse_response(payload: object) -> ApiResponse:
    """Validate the three fields required by the AUPU response contract."""
    if not isinstance(payload, dict):
        raise AupuProtocolError()
    if {"status", "result", "timestamp"} - payload.keys():
        raise AupuProtocolError()

    status = payload["status"]
    timestamp = payload["timestamp"]
    if isinstance(status, bool) or not isinstance(status, int):
        raise AupuProtocolError()
    if isinstance(timestamp, bool) or not isinstance(timestamp, int):
        raise AupuProtocolError()
    if status in _AUTH_STATUSES:
        raise AupuAuthError()
    if status != 0:
        raise AupuProtocolError()
    return ApiResponse(status=status, result=payload["result"], timestamp=timestamp)


def _parse_phone_login(result: object) -> PhoneLoginResult:
    """Return only the non-empty credential fields required for reauthentication."""
    if not isinstance(result, Mapping):
        raise AupuProtocolError()
    token = result.get("token")
    user = result.get("user")
    if not isinstance(token, str) or not token.strip() or not isinstance(user, Mapping):
        raise AupuProtocolError()
    user_uuid = user.get("userUuid")
    if not isinstance(user_uuid, str) or not user_uuid.strip():
        raise AupuProtocolError()
    return PhoneLoginResult(token=token.strip(), user_uuid=user_uuid.strip())
