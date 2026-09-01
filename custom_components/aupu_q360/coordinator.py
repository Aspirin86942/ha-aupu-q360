"""Authentication, Repair, and light-state coordination for AUPU Q360."""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import issue_registry as ir

from .api import AupuApiClient
from .auth import AuthState, BearerCredential
from .const import DOMAIN
from .errors import AupuAuthError, AupuError


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
    ) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._credential = credential
        self._api = api
        self._async_request_reauth = async_request_reauth
        self._reauth_requested = False
        self._is_on: bool | None = None
        self._assumed_state = False
        self._listeners: set[Callable[[], None]] = set()
        self._stopped = False

    @property
    def is_on(self) -> bool | None:
        """Return the latest desired or confirmed light state."""
        return self._is_on

    @property
    def assumed_state(self) -> bool:
        """Return whether the latest light state lacks physical confirmation."""
        return self._assumed_state

    async def async_start(self) -> None:
        """Reconcile JWT Repairs without creating background work."""
        self._stopped = False
        state = self._reconcile_repairs()
        if state is AuthState.EXPIRED:
            self._request_reauth_once()
            raise ConfigEntryAuthFailed("Authentication failed")

    async def async_stop(self) -> None:
        """Remove every owned listener; no polling task is created by this class."""
        self._stopped = True
        self._listeners.clear()

    async def async_set_light(self, is_on: bool) -> None:
        """Send exactly one control call after enforcing the local auth gate."""
        if self._stopped:
            raise HomeAssistantError("Light coordinator is stopped")
        if self._reconcile_repairs() is AuthState.EXPIRED:
            self._request_reauth_once()
            raise ConfigEntryAuthFailed("Authentication failed")

        try:
            await self._api.set_light(is_on)
        except AupuAuthError:
            self._request_reauth_once()
            raise ConfigEntryAuthFailed("Authentication failed") from None
        except AupuError:
            raise HomeAssistantError("Light control failed") from None

        self.async_apply_light_state(is_on=is_on, confirmed=False)

    @callback
    def _request_reauth_once(self) -> None:
        """Request one entry reauth flow for this runtime without passing secrets."""
        if self._reauth_requested:
            return
        self._reauth_requested = True
        self._async_request_reauth()

    @callback
    def async_apply_light_state(self, *, is_on: bool, confirmed: bool) -> None:
        """Apply desired or physically confirmed state and notify entities."""
        self._is_on = is_on
        self._assumed_state = not confirmed
        for listener in tuple(self._listeners):
            listener()

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
