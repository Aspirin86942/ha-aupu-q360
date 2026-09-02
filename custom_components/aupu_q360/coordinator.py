"""Authentication, Repair, and light-state coordination for AUPU Q360."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal

import aiohttp
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import issue_registry as ir

from .api import AupuApiClient
from .auth import AuthState, BearerCredential
from .const import DOMAIN
from .errors import AupuAuthError, AupuError
from .models import DeviceConfig
from .shadow import (
    AcceptedShadow,
    LightShadowUpdate,
    RawShadowEvent,
    parse_accepted_shadow,
    parse_light_shadow_update,
)
from .wss import AupuShadowWebSocket

LightStateSource = Literal["unknown", "command", "reported", "desired", "get_reported"]
StateClock = Callable[[], datetime]
DiscoveryObserver = Callable[[AcceptedShadow], None]
DiscoveryCancel = Callable[[], None]
OutgoingDiscoveryRecorder = Callable[[RawShadowEvent], None]
_LOGGER = logging.getLogger(__name__)


def _utc_now() -> datetime:
    """Return the current aware UTC timestamp for confirmed state evidence."""
    return datetime.now(UTC)


class AupuCoordinator:
    """Own authentication gates, Repair state, and the current light state."""

    def __init__(
        self,
        *,
        hass: HomeAssistant,
        entry_id: str,
        credential: BearerCredential,
        api: AupuApiClient,
        async_request_reauth: Callable[[], None],
        session: aiohttp.ClientSession | None = None,
        device: DeviceConfig | None = None,
        use_wss: bool = False,
        user_uuid: str | None = None,
        now: StateClock = _utc_now,
    ) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._credential = credential
        self._api = api
        self._async_request_reauth = async_request_reauth
        self._reauth_requested = False
        self._is_on: bool | None = None
        self._assumed_state = use_wss
        self._listeners: set[Callable[[], None]] = set()
        self._stopped = False
        self._wss_connected = False
        self._wss_healthy = False
        self._last_error_code = "none"
        self._light_state_source: LightStateSource = "unknown"
        self._now = now
        self._state_stale = True
        self._last_confirmed_at: datetime | None = None
        self._wss: AupuShadowWebSocket | None = None
        self._device = device
        self._discovery_observer: DiscoveryObserver | None = None
        self._discovery_cancel: DiscoveryCancel | None = None
        if use_wss:
            if session is None or device is None:
                raise ValueError("WSS runtime dependencies are required")
            self._wss = AupuShadowWebSocket(
                session=session,
                api=api,
                credential=credential,
                device=device,
                user_uuid=user_uuid,
                async_connection_changed=self.async_apply_wss_connection,
                async_auth_failed=self.async_handle_wss_auth_failure,
                parse_shadow=lambda topic, payload: parse_accepted_shadow(device, topic, payload),
                async_shadow_message=self.async_apply_shadow_message,
            )
            self._wss_missing_user_uuid = user_uuid is None
        else:
            self._wss_missing_user_uuid = False

    @property
    def is_on(self) -> bool | None:
        """Return the latest desired or confirmed light state."""
        return self._is_on

    @property
    def assumed_state(self) -> bool:
        """Return whether the latest light state lacks physical confirmation."""
        return self._assumed_state

    @property
    def wss_connected(self) -> bool:
        """Return whether the MQTT Shadow session completed subscriptions."""
        return self._wss_connected

    @property
    def wss_healthy(self) -> bool:
        """Return whether the current WSS session received a PINGRESP."""
        return self._wss_healthy

    @property
    def discovery_available(self) -> bool:
        """Return whether a read-only discovery snapshot can be sent now."""
        return (
            not self._stopped
            and not self._reauth_requested
            and self._wss is not None
            and self._wss_connected
        )

    @property
    def last_error_code(self) -> str:
        """Return only the latest fixed, credential-safe runtime error code."""
        return self._last_error_code

    @property
    def light_state_source(self) -> LightStateSource:
        """Return the fixed source category for the latest light state."""
        return self._light_state_source

    @property
    def state_stale(self) -> bool:
        """Return whether the latest light state lacks a current confirmation."""
        return self._state_stale

    @property
    def last_confirmed_at(self) -> datetime | None:
        """Return the UTC instant of the last physically confirmed light state."""
        return self._last_confirmed_at

    async def async_start(self) -> None:
        """Reconcile JWT Repairs and start the optional WSS state channel."""
        self._stopped = False
        state = self._reconcile_repairs()
        if state is AuthState.EXPIRED:
            self._last_error_code = AupuAuthError.error_code
            self._request_reauth_once()
            raise ConfigEntryAuthFailed("Authentication failed")
        if self._wss_missing_user_uuid:
            self._last_error_code = AupuAuthError.error_code
            self._request_reauth_once()
        if self._wss is not None:
            await self._wss.async_start()

    async def async_stop(self) -> None:
        """Stop the optional WSS channel and remove every owned listener."""
        self._stopped = True
        try:
            if self._wss is not None:
                await self._wss.async_stop()
        finally:
            self._wss_connected = False
            self._wss_healthy = False
            self._state_stale = True
            self._listeners.clear()
            self._discovery_observer = None
            self._discovery_cancel = None

    async def async_request_shadow_get(
        self,
        client_token: str,
        record_outgoing: OutgoingDiscoveryRecorder | None = None,
    ) -> None:
        """Send one read-only snapshot request through the existing WSS."""
        if not self.discovery_available or self._wss is None:
            raise HomeAssistantError("discovery_wss_unavailable")
        try:
            await self._wss.async_request_shadow_get(client_token, record_outgoing)
        except (AupuError, aiohttp.ClientError, RuntimeError, TimeoutError):
            raise HomeAssistantError("discovery_wss_unavailable") from None

    async def async_set_light(self, is_on: bool) -> None:
        """Send exactly one control call after enforcing the local auth gate."""
        if self._stopped:
            self._last_error_code = "runtime_stopped"
            raise HomeAssistantError("Light coordinator is stopped")
        if self._reconcile_repairs() is AuthState.EXPIRED:
            self._last_error_code = AupuAuthError.error_code
            self._request_reauth_once()
            raise ConfigEntryAuthFailed("Authentication failed")

        try:
            await self._api.set_light(is_on)
        except AupuAuthError:
            self._last_error_code = AupuAuthError.error_code
            self._request_reauth_once()
            raise ConfigEntryAuthFailed("Authentication failed") from None
        except AupuError as exc:
            self._last_error_code = exc.error_code
            raise HomeAssistantError("Light control failed") from None

        self.async_apply_light_state(is_on=is_on, source="command")

    @callback
    def _request_reauth_once(self) -> None:
        """Request one entry reauth flow for this runtime without passing secrets."""
        if self._reauth_requested:
            return
        self._reauth_requested = True
        self._async_request_reauth()

    @callback
    def async_apply_light_state(
        self,
        *,
        is_on: bool,
        source: LightStateSource = "unknown",
    ) -> None:
        """Apply desired or physically confirmed state and notify entities."""
        confirmed = source in ("reported", "get_reported")
        confirmed_at: datetime | None = None
        if confirmed:
            candidate = self._now()
            if candidate.tzinfo is None or candidate.utcoffset() is None:
                raise ValueError("State clock must return an aware datetime")
            confirmed_at = candidate.astimezone(UTC)

        self._is_on = is_on
        self._assumed_state = not confirmed
        self._light_state_source = source
        self._state_stale = not confirmed
        if confirmed_at is not None:
            self._last_confirmed_at = confirmed_at
        for listener in tuple(self._listeners):
            listener()

    @callback
    def async_apply_shadow_update(self, update: LightShadowUpdate) -> None:
        """Apply only reported sources as physical confirmation."""
        self.async_apply_light_state(
            is_on=update.is_on,
            source=update.source,
        )

    @callback
    def async_apply_shadow_message(self, message: AcceptedShadow) -> None:
        """Apply formal light state before isolating optional discovery work."""
        if self._device is None:
            return
        update = parse_light_shadow_update(self._device, message)
        if update is not None:
            self.async_apply_shadow_update(update)
        observer = self._discovery_observer
        if observer is None:
            return
        try:
            observer(message)
        except Exception:  # noqa: BLE001 - discovery must not affect formal state
            _LOGGER.error("AUPU discovery observer failed")

    @callback
    def async_set_discovery_observer(
        self,
        observer: DiscoveryObserver,
        cancel: DiscoveryCancel,
    ) -> Callable[[], None]:
        """Attach the sole active discovery observer and return its remover."""
        if self._discovery_observer is not None:
            raise HomeAssistantError("discovery_busy")
        self._discovery_observer = observer
        self._discovery_cancel = cancel

        @callback
        def remove() -> None:
            if self._discovery_observer is observer:
                self._discovery_observer = None
                self._discovery_cancel = None

        return remove

    @callback
    def async_apply_wss_connection(self, connected: bool, healthy: bool) -> None:
        """Update WSS availability without clearing or reversing the light state."""
        self._wss_connected = connected
        self._wss_healthy = connected and healthy
        if not connected:
            self._state_stale = True
            cancel = self._discovery_cancel
            if cancel is not None:
                cancel()
        for listener in tuple(self._listeners):
            listener()

    @callback
    def async_handle_wss_auth_failure(self) -> None:
        """Route transport authentication failure through the one-shot Reauth gate."""
        self._last_error_code = AupuAuthError.error_code
        cancel = self._discovery_cancel
        if cancel is not None:
            cancel()
        self._request_reauth_once()

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Register one entity state listener and return its removal callback."""
        self._listeners.add(listener)

        @callback
        def remove_listener() -> None:
            self._listeners.discard(listener)

        return remove_listener

    @callback
    def _reconcile_repairs(self) -> AuthState:
        """Make this entry's Repair issues match its current JWT state."""
        state = self._credential.state()
        if getattr(self._hass, "data", None) is None:
            return state
        expiring_id = f"{self._entry_id}_jwt_expiring"
        expired_id = f"{self._entry_id}_jwt_expired"

        if state is AuthState.READY:
            ir.async_delete_issue(self._hass, DOMAIN, expiring_id)
            ir.async_delete_issue(self._hass, DOMAIN, expired_id)
        elif state is AuthState.EXPIRING:
            ir.async_delete_issue(self._hass, DOMAIN, expired_id)
            ir.async_create_issue(
                self._hass,
                DOMAIN,
                expiring_id,
                is_fixable=False,
                is_persistent=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="jwt_expiring",
            )
        else:
            ir.async_delete_issue(self._hass, DOMAIN, expiring_id)
            ir.async_create_issue(
                self._hass,
                DOMAIN,
                expired_id,
                is_fixable=False,
                is_persistent=True,
                severity=ir.IssueSeverity.ERROR,
                translation_key="jwt_expired",
            )
        return state
