"""Local lifecycle hints for opaque Bearer JWT credentials."""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from .errors import AupuAuthError

_EXPIRING_WINDOW = timedelta(hours=24)


class AuthState(StrEnum):
    """Local token lifecycle state derived solely from an unverified exp claim."""

    READY = "ready"
    EXPIRING = "expiring"
    EXPIRED = "expired"


@dataclass(frozen=True, repr=False)
class BearerCredential:
    """An opaque Bearer credential with an optional local expiration hint.

    JWT payload data is decoded only to obtain ``exp``. This class does not validate
    the JWT signature, identity, permissions, or any other claim.
    """

    _token: str
    expires_at: datetime | None

    @classmethod
    def parse(cls, value: str) -> BearerCredential:
        """Parse a JWT payload for its optional expiry without trusting its claims."""
        token = value.removeprefix("Bearer ") if isinstance(value, str) else ""
        if not token:
            raise _invalid_credential()

        parts = token.split(".")
        if len(parts) != 3:
            raise _invalid_credential()

        try:
            payload = _decode_payload(parts[1])
            exp = payload.get("exp")
            expires_at = _expiry_from_exp(exp) if exp is not None else None
        except (TypeError, ValueError, OverflowError, json.JSONDecodeError, binascii.Error):
            raise _invalid_credential() from None
        return cls(_token=token, expires_at=expires_at)

    @property
    def authorization_header(self) -> str:
        """Return the caller's original credential in the HTTP Bearer form."""
        return f"Bearer {self._token}"

    def state(self, now: datetime | None = None) -> AuthState:
        """Return only the local lifecycle state implied by the unverified expiry."""
        if self.expires_at is None:
            return AuthState.READY

        current = datetime.now(UTC) if now is None else _as_utc(now)
        if current >= self.expires_at:
            return AuthState.EXPIRED
        if self.expires_at - current < _EXPIRING_WINDOW:
            return AuthState.EXPIRING
        return AuthState.READY

    def __repr__(self) -> str:
        """Never include credential text in debugging output."""
        return f"{type(self).__name__}(token=<redacted>, expires_at={self.expires_at!r})"


def _decode_payload(value: str) -> dict[str, Any]:
    """Decode exactly the JSON object in the payload segment of a compact JWT."""
    padding = "=" * (-len(value) % 4)
    normalized = value.replace("-", "+").replace("_", "/")
    decoded = base64.b64decode(normalized + padding, validate=True)
    payload = json.loads(decoded)
    if not isinstance(payload, dict):
        raise TypeError("JWT payload must be an object")
    return payload


def _expiry_from_exp(value: object) -> datetime:
    """Convert a numeric unverified exp timestamp into a local UTC hint."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError("JWT exp must be numeric")
    return datetime.fromtimestamp(value, UTC)


def _as_utc(value: datetime) -> datetime:
    """Interpret naive injected test clocks as UTC and normalize aware clocks."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _invalid_credential() -> AupuAuthError:
    """Return a fixed error that cannot include an untrusted credential."""
    return AupuAuthError()
