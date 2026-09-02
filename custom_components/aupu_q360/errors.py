"""Safe, categorized errors for the AUPU Q360 integration."""

from __future__ import annotations

from typing import ClassVar

from homeassistant.exceptions import HomeAssistantError


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
            f"{type(self).__name__}(error_code={self.error_code!r}, retryable={self.retryable!r})"
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


class DiscoveryError(HomeAssistantError):
    """Base class whose public text is always one controlled error code."""

    error_code: ClassVar[str]

    def __init__(self) -> None:
        super().__init__(self.error_code)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(error_code={self.error_code!r})"


class DiscoveryWssUnavailableError(DiscoveryError):
    """The sole WSS transport is not subscribed and ready."""

    error_code = "discovery_wss_unavailable"


class DiscoveryBusyError(DiscoveryError):
    """A discovery session already owns this Config Entry."""

    error_code = "discovery_busy"


class DiscoverySnapshotTimeoutError(DiscoveryError):
    """A correlated full reported snapshot did not arrive in time."""

    error_code = "discovery_snapshot_timeout"


class DiscoveryInvalidTransitionError(DiscoveryError):
    """The requested action is not valid in the current controlled state."""

    error_code = "discovery_invalid_transition"


class DiscoveryStepExpiredError(DiscoveryError):
    """The active panel experiment exceeded its fixed deadline."""

    error_code = "discovery_step_expired"


class DiscoverySessionExpiredError(DiscoveryError):
    """The complete discovery session exceeded its fixed deadline."""

    error_code = "discovery_session_expired"


class DiscoveryResourceLimitError(DiscoveryError):
    """The active step exceeded a bounded sanitization resource."""

    error_code = "discovery_resource_limit"


class DiscoveryReportSaveFailedError(DiscoveryError):
    """The final sanitized report could not atomically replace the prior one."""

    error_code = "discovery_report_save_failed"
