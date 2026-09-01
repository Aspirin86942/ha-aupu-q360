"""Tests for local-only Bearer token lifecycle handling."""

from __future__ import annotations

import base64
import json
import math
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from custom_components.aupu_q360.auth import AuthState, BearerCredential
from custom_components.aupu_q360.errors import (
    AupuAuthError,
    AupuError,
    AupuProtocolError,
    AupuRateLimitError,
    AupuTemporaryError,
)


def make_synthetic_jwt(payload: object) -> str:
    """Build an unsigned fixture token; production code must not trust its claims."""
    header = {"alg": "none", "typ": "JWT"}

    def encode(value: object) -> str:
        encoded = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(encoded).rstrip(b"=").decode("ascii")

    return f"{encode(header)}.{encode(payload)}.synthetic-signature"


def test_token_lifecycle_uses_exp_without_treating_claims_as_verified() -> None:
    """Changing local clock state must not turn decoded claims into verified identity."""
    token = make_synthetic_jwt({"exp": 1_700_086_400, "sub": "synthetic"})

    credential = BearerCredential.parse(token)

    assert credential.authorization_header.startswith("Bearer ")
    assert credential.expires_at == datetime.fromtimestamp(1_700_086_400, UTC)
    assert credential.state(now=datetime.fromtimestamp(1_700_000_000, UTC)) is AuthState.READY
    assert not hasattr(credential, "claims")


def test_existing_bearer_prefix_is_not_duplicated() -> None:
    """A caller-supplied authorization value must remain a single Bearer header."""
    token = make_synthetic_jwt({"exp": 1_700_086_400})

    credential = BearerCredential.parse(f"Bearer {token}")

    assert credential.authorization_header == f"Bearer {token}"


def _assert_rejected_without_token_disclosure(make_token: Callable[[], str]) -> None:
    """Assert a fixed auth failure without letting pytest render a credential."""
    token = make_token()
    try:
        BearerCredential.parse(token)
    except AupuAuthError as error:
        diagnostics = [str(error), repr(error)]
        current: BaseException | None = error.__cause__ or error.__context__
        while current is not None:
            diagnostics.extend((str(current), repr(current)))
            current = current.__cause__ or current.__context__
        if any(token in diagnostic for diagnostic in diagnostics):
            pytest.fail("Bearer credential appeared in authentication diagnostics")
    else:
        pytest.fail("Invalid Bearer credential was accepted")


def test_token_without_exp_is_rejected() -> None:
    """Deleting required-exp validation must fail this fail-closed behavior."""
    _assert_rejected_without_token_disclosure(lambda: make_synthetic_jwt({"sub": "synthetic"}))


def test_token_with_null_exp_is_rejected() -> None:
    """Treating JSON null like an optional expiry must fail closed."""
    _assert_rejected_without_token_disclosure(lambda: make_synthetic_jwt({"exp": None}))


@pytest.mark.parametrize("exp", [True, "1700086400", math.nan, math.inf])
def test_token_with_invalid_exp_is_rejected(exp: object) -> None:
    """Accepting non-numeric or non-finite expiry values would weaken lifecycle checks."""
    _assert_rejected_without_token_disclosure(lambda: make_synthetic_jwt({"exp": exp}))


def test_repeated_bearer_prefix_is_rejected() -> None:
    """A second prefix must be rejected instead of leaking into an HTTP header."""
    _assert_rejected_without_token_disclosure(
        lambda: f"Bearer Bearer {make_synthetic_jwt({'exp': 1_700_086_400})}"
    )


def test_invalid_payload_raises_redacted_auth_error() -> None:
    """Malformed inputs must fail closed without copying a credential into diagnostics."""
    malformed_tokens = (
        "eyJhbGciOiJub25lIn0.not-base64.signature",
        "eyJhbGciOiJub25lIn0.%%% .signature".replace(" ", ""),
        make_synthetic_jwt("not-a-json-object"),
    )
    for token in malformed_tokens:
        _assert_rejected_without_token_disclosure(lambda token=token: token)


def test_token_is_expiring_during_last_twenty_four_hours() -> None:
    """A lifecycle threshold off by one day would stop warning before local expiry."""
    credential = BearerCredential.parse(make_synthetic_jwt({"exp": 1_700_086_400}))

    assert credential.state(now=datetime.fromtimestamp(1_700_000_001, UTC)) is AuthState.EXPIRING


def test_token_is_expired_at_its_expiration_instant() -> None:
    """An expiry comparison using greater-than instead of greater-or-equal is unsafe."""
    credential = BearerCredential.parse(make_synthetic_jwt({"exp": 1_700_086_400}))

    assert credential.state(now=datetime.fromtimestamp(1_700_086_400, UTC)) is AuthState.EXPIRED


@pytest.mark.parametrize(
    ("error", "error_code", "message", "retryable"),
    [
        (AupuAuthError(), "authentication_failed", "Authentication failed", False),
        (AupuRateLimitError(), "rate_limited", "Request rate is limited", True),
        (AupuTemporaryError(), "temporary_failure", "Temporary service failure", True),
        (AupuProtocolError(), "protocol_error", "Service response is invalid", False),
    ],
)
def test_error_categories_expose_only_stable_safe_details(
    error: AupuError, error_code: str, message: str, retryable: bool
) -> None:
    """Callers need stable categorization without credential-bearing exception text."""
    assert error.error_code == error_code
    assert error.message == message
    assert error.retryable is retryable
    assert str(error) == message
    assert error_code in repr(error)
