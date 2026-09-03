"""Authentication, Repair, and light-state coordination for AUPU Q360."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
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
    PanelFieldUpdate,
    PanelMode,
    PanelStateUpdate,
    parse_accepted_shadow,
    parse_light_shadow_update,
    parse_panel_shadow_update,
)
from .wss import AupuShadowWebSocket

LightStateSource = Literal["unknown", "command", "reported", "desired", "get_reported"]
StateClock = Callable[[], datetime]


@dataclass(slots=True)
class _PanelFieldState[T]:
    value: T | None = None
    fresh: bool = False

    def apply(self, update: PanelFieldUpdate[T]) -> bool:
        if not update.present:
            return False
        self.value = update.value
        self.fresh = update.value is not None
        return True

    def mark_stale(self) -> None:
        self.fresh = False


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
        self._panel_mode = _PanelFieldState[PanelMode]()
        self._night_light = _PanelFieldState[bool]()
        self._fan_level = _PanelFieldState[int]()
        self._ai_target_temperature = _PanelFieldState[int]()
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
    def panel_mode(self) -> PanelMode | None:
        """Return the latest normalized panel mode."""
        return self._panel_mode.value

    @property
    def night_light_is_on(self) -> bool | None:
        """Return the latest normalized night-light state."""
        return self._night_light.value

    @property
    def fan_level(self) -> int | None:
        """Return the latest normalized fan level."""
        return self._fan_level.value

    @property
    def ai_target_temperature(self) -> int | None:
        """Return the latest normalized AI target temperature."""
        return self._ai_target_temperature.value

    def _panel_field_available[T](self, field: _PanelFieldState[T]) -> bool:
        return self._wss_connected and field.fresh and field.value is not None

    @property
    def panel_mode_available(self) -> bool:
        """Return whether the current connection confirmed a usable mode."""
        return self._panel_field_available(self._panel_mode)

    @property
    def night_light_available(self) -> bool:
        """Return whether the current connection confirmed the night light."""
        return self._panel_field_available(self._night_light)

    @property
    def fan_level_available(self) -> bool:
        """Return whether the current connection confirmed the fan level."""
        return self._panel_field_available(self._fan_level)

    @property
    def ai_target_temperature_available(self) -> bool:
        """Return whether the current connection confirmed the target temperature."""
        return self._panel_field_available(self._ai_target_temperature)

    @property
    def panel_state_available(self) -> bool:
        """Return whether any formal panel field is currently usable."""
        return any(
            (
                self.panel_mode_available,
                self.night_light_available,
                self.fan_level_available,
                self.ai_target_temperature_available,
            )
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
            self._mark_panel_state_stale()
            self._listeners.clear()

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
        self._apply_light_state(is_on=is_on, source=source)
        self._notify_listeners()

    def _apply_light_state(
        self,
        *,
        is_on: bool,
        source: LightStateSource,
    ) -> None:
        """Apply light state without notifying listeners."""
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

    def _notify_listeners(self) -> None:
        """Notify a stable snapshot of registered listeners."""
        for listener in tuple(self._listeners):
            listener()

    def _apply_panel_state_update(self, update: PanelStateUpdate) -> bool:
        changed = False
        changed |= self._panel_mode.apply(update.mode)
        changed |= self._night_light.apply(update.night_light)
        changed |= self._fan_level.apply(update.fan_level)
        changed |= self._ai_target_temperature.apply(update.ai_target_temperature)
        return changed

    def _mark_panel_state_stale(self) -> None:
        self._panel_mode.mark_stale()
        self._night_light.mark_stale()
        self._fan_level.mark_stale()
        self._ai_target_temperature.mark_stale()

    @callback
    def async_apply_shadow_update(self, update: LightShadowUpdate) -> None:
        """Apply only reported sources as physical confirmation."""
        self.async_apply_light_state(
            is_on=update.is_on,
            source=update.source,
        )

    @callback
    def async_apply_shadow_message(self, message: AcceptedShadow) -> None:
        """Apply all formal state from one Shadow message before notifying."""
        if self._device is None:
            return
        light_update = parse_light_shadow_update(self._device, message)
        panel_update = parse_panel_shadow_update(self._device, message)
        applied = False
        if light_update is not None:
            self._apply_light_state(
                is_on=light_update.is_on,
                source=light_update.source,
            )
            applied = True
        if panel_update is not None:
            applied |= self._apply_panel_state_update(panel_update)
        if applied:
            self._notify_listeners()

    @callback
    def async_apply_wss_connection(self, connected: bool, healthy: bool) -> None:
        """Update WSS availability without clearing or reversing the light state."""
        self._wss_connected = connected
        self._wss_healthy = connected and healthy
        if not connected:
            self._state_stale = True
            self._mark_panel_state_stale()
        self._notify_listeners()

    @callback
    def async_handle_wss_auth_failure(self) -> None:
        """Route transport authentication failure through the one-shot Reauth gate."""
        self._last_error_code = AupuAuthError.error_code
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
