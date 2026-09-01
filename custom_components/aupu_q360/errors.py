"""Safe, categorized errors for the AUPU Q360 integration."""

from __future__ import annotations

from typing import ClassVar


class AupuError(Exception):
    """Base error with fixed, credential-safe details for callers."""

    error_code: ClassVar[str] = "aupu_error"
    message: ClassVar[str] = "AUPU integration error"
    retryable: ClassVar[bool] = False

    def __init__(self) -> None:
        """Keep exception text fixed so untrusted input cannot be echoed."""
        super().__init__(self.message)

    def __repr__(self) -> str:
        """Provide safe diagnostics without incorporating exception context."""
        return (
            f"{type(self).__name__}(error_code={self.error_code!r}, "
            f"retryable={self.retryable!r})"
        )


class AupuAuthError(AupuError):
    """The locally supplied authentication credential cannot be used."""

    error_code = "authentication_failed"
    message = "Authentication failed"


class AupuRateLimitError(AupuError):
    """The remote service has temporarily limited requests."""

    error_code = "rate_limited"
    message = "Request rate is limited"
    retryable = True


class AupuTemporaryError(AupuError):
    """A temporary remote failure may succeed after a later retry."""

    error_code = "temporary_failure"
    message = "Temporary service failure"
    retryable = True


class AupuProtocolError(AupuError):
    """The remote service response does not match the expected protocol."""

    error_code = "protocol_error"
    message = "Service response is invalid"
