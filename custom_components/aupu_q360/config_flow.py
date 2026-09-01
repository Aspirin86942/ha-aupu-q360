"""Local-first config and options flows for AUPU Q360."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from time import monotonic
from typing import Any, cast

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import AupuApiClient
from .auth import AuthState, BearerCredential
from .const import DOMAIN
from .errors import AupuError, AupuProtocolError
from .models import AupuConfigEntryData, DeviceConfig
from .signer import AppAuthorizationSigner, SignerSecrets

_CONF_SIGNER_JSON = "signer_json"
_CONF_SIGNER = "signer"
_CONF_TOKEN = "token"
_CONF_DID = "did"
_CONF_TAG = "tag"
_CONF_USE_WSS = "use_wss"
_CONF_USER_UUID = "user_uuid"
_CONF_PHONE = "phone"
_CONF_METHOD = "method"
_CONF_SMS_CODE = "code"
_CONF_SAVE_PHONE = "save_phone"

_SMS_RESEND_SECONDS = 60
_SMS_CODE_TTL_SECONDS = 5 * 60

_TERMINAL_INFO_PATH = "/authserver/auth/user/terminal/info"
_ENTRY_TITLE = "AUPU Q360"

_SECRET_TEXT = selector.TextSelector(
    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
)
_SECRET_JSON = selector.TextSelector(
    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD, multiline=True)
)


def _user_schema() -> vol.Schema:
    """Build a schema that never pre-populates private material."""
    return vol.Schema(
        {
            vol.Required(_CONF_SIGNER_JSON): _SECRET_JSON,
            vol.Required(_CONF_TOKEN): _SECRET_TEXT,
            vol.Required(_CONF_DID): str,
            vol.Required(_CONF_TAG): str,
            vol.Optional(_CONF_USE_WSS, default=False): selector.BooleanSelector(),
        }
    )


def _confirm_schema() -> vol.Schema:
    """Return an explicit submit-only confirmation schema."""
    return vol.Schema({})


def _reauth_manual_token_schema() -> vol.Schema:
    """Request a new token without displaying the persisted credential."""
    return vol.Schema({vol.Required(_CONF_TOKEN): _SECRET_TEXT})


def _reauth_method_schema() -> vol.Schema:
    """Offer the two supported recovery methods without exposing credentials."""
    return vol.Schema({vol.Required(_CONF_METHOD): vol.In(("sms", "manual_token"))})


def _reauth_sms_send_schema(current: AupuConfigEntryData) -> vol.Schema:
    """Collect a phone number only after the user chooses SMS recovery."""
    return vol.Schema(
        {
            vol.Required(_CONF_PHONE, default=current.phone or ""): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.TEL)
            ),
            vol.Optional(_CONF_SAVE_PHONE, default=False): selector.BooleanSelector(),
        }
    )


def _reauth_sms_code_schema() -> vol.Schema:
    """Collect the ephemeral verification code without displaying it later."""
    return vol.Schema({vol.Required(_CONF_SMS_CODE): _SECRET_TEXT})


def _options_schema(current: AupuConfigEntryData) -> vol.Schema:
    """Build options without echoing the persisted token into the form."""
    return vol.Schema(
        {
            vol.Optional(_CONF_TOKEN, default=""): _SECRET_TEXT,
            vol.Optional(_CONF_PHONE, default=current.phone or ""): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.TEL)
            ),
            vol.Optional(_CONF_USE_WSS, default=current.use_wss): selector.BooleanSelector(),
        }
    )


class _ExpiredTokenError(ValueError):
    """Separate a valid-but-expired token from malformed input."""


class _InvalidSignerError(ValueError):
    """Signal signer validation without retaining submitted secret material."""


class _InvalidDeviceError(ValueError):
    """Signal device validation without retaining submitted identifiers."""


class _InvalidPhoneError(ValueError):
    """Signal local phone validation without retaining the submitted value."""


class _InvalidSmsCodeError(ValueError):
    """Signal local code validation without retaining the submitted value."""


def _parse_token(value: object) -> str:
    """Return one canonical, unexpired JWT representation."""
    credential = BearerCredential.parse(cast(str, value))
    if credential.state() is AuthState.EXPIRED:
        raise _ExpiredTokenError
    return credential.authorization_header.removeprefix("Bearer ")


def _parse_phone(value: object) -> str:
    """Require the supported eleven-digit local phone representation."""
    if not isinstance(value, str):
        raise _InvalidPhoneError
    phone = value.strip()
    if len(phone) != 11 or not all("0" <= character <= "9" for character in phone):
        raise _InvalidPhoneError
    return phone


def _parse_sms_code(value: object) -> str:
    """Require exactly six digits and keep the value in the flow only."""
    if (
        not isinstance(value, str)
        or len(value) != 6
        or not all("0" <= character <= "9" for character in value)
    ):
        raise _InvalidSmsCodeError
    return value


def _parse_user_input(user_input: Mapping[str, Any]) -> AupuConfigEntryData:
    """Validate every initial field without performing I/O."""
    signer_json = user_input.get(_CONF_SIGNER_JSON)
    if not isinstance(signer_json, str):
        raise _InvalidSignerError
    try:
        signer_value = json.loads(signer_json)
    except json.JSONDecodeError:
        raise _InvalidSignerError from None
    if not isinstance(signer_value, dict):
        raise _InvalidSignerError
    try:
        SignerSecrets.from_mapping(signer_value)
    except (TypeError, ValueError):
        raise _InvalidSignerError from None

    token = _parse_token(user_input.get(_CONF_TOKEN))
    did_value = user_input.get(_CONF_DID)
    tag_value = user_input.get(_CONF_TAG)
    if not isinstance(did_value, str) or not isinstance(tag_value, str):
        raise _InvalidDeviceError
    try:
        device = DeviceConfig(did=did_value, tag=tag_value)
    except (TypeError, ValueError):
        raise _InvalidDeviceError from None

    use_wss = user_input.get(_CONF_USE_WSS, False)
    if not isinstance(use_wss, bool):
        raise _InvalidDeviceError
    return AupuConfigEntryData.from_mapping(
        {
            _CONF_SIGNER: signer_value,
            _CONF_TOKEN: token,
            _CONF_DID: device.did,
            _CONF_TAG: device.tag,
            _CONF_USE_WSS: use_wss,
        },
        require_user_uuid=False,
    )


def _local_error(exc: Exception) -> str:
    """Map local validation failures to fixed, non-sensitive UI keys."""
    if isinstance(exc, _InvalidSignerError):
        return "invalid_signer"
    if isinstance(exc, _ExpiredTokenError):
        return "expired_token"
    if isinstance(exc, _InvalidDeviceError):
        return "invalid_device"
    if isinstance(exc, _InvalidPhoneError):
        return "invalid_phone"
    if isinstance(exc, _InvalidSmsCodeError):
        return "invalid_sms_code"
    return "invalid_token"


async def _async_verify_terminal_info(
    hass: HomeAssistant, candidate: AupuConfigEntryData
) -> str:
    """Perform exactly one confirmed read-only request and extract user UUID."""
    signer = AppAuthorizationSigner(candidate.secrets)
    api = AupuApiClient(
        session=async_get_clientsession(hass),
        signer=signer,
        credential=candidate.credential,
        device=candidate.device,
    )
    response = await api.request("GET", _TERMINAL_INFO_PATH, json={})
    result = response.result
    if not isinstance(result, Mapping):
        raise AupuProtocolError()
    content = result.get("content")
    if not isinstance(content, Mapping):
        raise AupuProtocolError()
    user_uuid = content.get("userUuid")
    if not isinstance(user_uuid, str) or not user_uuid.strip():
        raise AupuProtocolError()
    return user_uuid.strip()


class AupuConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Configure one Q360 device from local private values."""

    VERSION = 1

    def __init__(self) -> None:
        self._pending: AupuConfigEntryData | None = None
        self._reauth_phone: str | None = None
        self._reauth_save_phone = False
        self._sms_sent_at: float | None = None

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry[Any],
    ) -> config_entries.OptionsFlow:
        """Return the options flow for atomic credential updates."""
        del config_entry
        return AupuOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Validate locally and create HTTPS-only entries by default."""
        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=_user_schema())
        try:
            candidate = _parse_user_input(user_input)
        except (AupuError, TypeError, ValueError) as exc:
            return self.async_show_form(
                step_id="user",
                data_schema=_user_schema(),
                errors={"base": _local_error(exc)},
            )

        unique_id = hashlib.sha256(candidate.did.encode()).hexdigest()[:20]
        existing = await self.async_set_unique_id(unique_id)
        if existing is not None:
            return self.async_abort(reason="already_configured")

        if candidate.use_wss:
            self._pending = candidate
            return self.async_show_form(
                step_id="confirm_wss", data_schema=_confirm_schema()
            )
        return self._create_entry(candidate)

    async def async_step_confirm_wss(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Contact the read-only terminal endpoint only after explicit submit."""
        if self._pending is None:
            return self.async_abort(reason="invalid_state")
        if user_input is None:
            return self.async_show_form(
                step_id="confirm_wss", data_schema=_confirm_schema()
            )
        try:
            user_uuid = await _async_verify_terminal_info(self.hass, self._pending)
            candidate = AupuConfigEntryData.from_mapping(
                {**self._pending.as_mapping(), _CONF_USER_UUID: user_uuid}
            )
        except (AupuError, TypeError, ValueError):
            return self.async_show_form(
                step_id="confirm_wss",
                data_schema=_confirm_schema(),
                errors={"base": "cannot_connect"},
            )
        return self._create_entry(candidate)

    async def async_step_reauth(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Ignore HA's initial entry payload and let the user select a recovery method."""
        del user_input
        return await self.async_step_reauth_method()

    async def async_step_reauth_method(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Select explicit SMS or manual-token recovery without sending a request."""
        if user_input is None:
            return self.async_show_form(
                step_id="reauth_method", data_schema=_reauth_method_schema()
            )
        method = user_input.get(_CONF_METHOD)
        if method == "sms":
            return await self.async_step_reauth_sms_send()
        if method == "manual_token":
            return await self.async_step_reauth_manual_token()
        return self.async_show_form(
            step_id="reauth_method",
            data_schema=_reauth_method_schema(),
            errors={"base": "invalid_state"},
        )

    async def async_step_reauth_sms_send(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Send one SMS only after submission and remember its local expiry window."""
        entry = self._get_reauth_entry()
        try:
            current = AupuConfigEntryData.from_mapping(
                entry.data, require_unexpired_token=False
            )
        except (AupuError, TypeError, ValueError):
            return self.async_abort(reason="invalid_entry")
        if user_input is None:
            return self.async_show_form(
                step_id="reauth_sms_send", data_schema=_reauth_sms_send_schema(current)
            )
        try:
            phone = _parse_phone(user_input.get(_CONF_PHONE))
            save_phone = user_input.get(_CONF_SAVE_PHONE, False)
            if not isinstance(save_phone, bool):
                raise _InvalidPhoneError
        except (TypeError, ValueError) as exc:
            return self.async_show_form(
                step_id="reauth_sms_send",
                data_schema=_reauth_sms_send_schema(current),
                errors={"base": _local_error(exc)},
            )
        now = monotonic()
        if self._sms_sent_at is not None and now - self._sms_sent_at < _SMS_RESEND_SECONDS:
            return self.async_show_form(
                step_id="reauth_sms_send",
                data_schema=_reauth_sms_send_schema(current),
                errors={"base": "sms_rate_limited"},
            )
        api = AupuApiClient(
            session=async_get_clientsession(self.hass),
            signer=AppAuthorizationSigner(current.secrets),
            credential=current.credential,
            device=current.device,
        )
        try:
            await api.request_sms_code(phone=phone)
        except (AupuError, TypeError, ValueError):
            return self.async_show_form(
                step_id="reauth_sms_send",
                data_schema=_reauth_sms_send_schema(current),
                errors={"base": "cannot_send_sms"},
            )
        self._reauth_phone = phone
        self._reauth_save_phone = save_phone
        self._sms_sent_at = now
        return await self.async_step_reauth_sms_code()

    async def async_step_reauth_sms_code(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Exchange one valid code and atomically replace only complete credentials."""
        if self._reauth_phone is None or self._sms_sent_at is None:
            return self.async_abort(reason="invalid_state")
        if user_input is None:
            return self.async_show_form(
                step_id="reauth_sms_code", data_schema=_reauth_sms_code_schema()
            )
        if monotonic() - self._sms_sent_at >= _SMS_CODE_TTL_SECONDS:
            return self.async_show_form(
                step_id="reauth_sms_code",
                data_schema=_reauth_sms_code_schema(),
                errors={"base": "sms_code_expired"},
            )
        try:
            code = _parse_sms_code(user_input.get(_CONF_SMS_CODE))
        except (TypeError, ValueError) as exc:
            return self.async_show_form(
                step_id="reauth_sms_code",
                data_schema=_reauth_sms_code_schema(),
                errors={"base": _local_error(exc)},
            )
        entry = self._get_reauth_entry()
        try:
            current = AupuConfigEntryData.from_mapping(
                entry.data, require_unexpired_token=False
            )
            api = AupuApiClient(
                session=async_get_clientsession(self.hass),
                signer=AppAuthorizationSigner(current.secrets),
                credential=current.credential,
                device=current.device,
            )
            login = await api.login_by_phone(self._reauth_phone, code)
            candidate_data = {
                **current.as_mapping(),
                _CONF_TOKEN: _parse_token(login.token),
                _CONF_USER_UUID: login.user_uuid,
            }
            if self._reauth_save_phone:
                candidate_data[_CONF_PHONE] = self._reauth_phone
            candidate = AupuConfigEntryData.from_mapping(candidate_data)
        except (AupuError, TypeError, ValueError):
            return self.async_show_form(
                step_id="reauth_sms_code",
                data_schema=_reauth_sms_code_schema(),
                errors={"base": "cannot_connect"},
            )
        return self._async_finish_reauth(entry, candidate)

    async def async_step_reauth_manual_token(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Atomically replace an expired or rejected token and reload once."""
        entry = self._get_reauth_entry()
        try:
            current = AupuConfigEntryData.from_mapping(
                entry.data, require_unexpired_token=False
            )
        except (AupuError, TypeError, ValueError):
            return self.async_abort(reason="invalid_entry")
        if user_input is None:
            return self.async_show_form(
                step_id="reauth_manual_token",
                data_schema=_reauth_manual_token_schema(),
            )

        try:
            token = _parse_token(user_input.get(_CONF_TOKEN))
            if token == current.token:
                raise ValueError
            candidate = AupuConfigEntryData.from_mapping(
                {**current.as_mapping(), _CONF_TOKEN: token}
            )
        except (AupuError, TypeError, ValueError) as exc:
            return self.async_show_form(
                step_id="reauth_manual_token",
                data_schema=_reauth_manual_token_schema(),
                errors={"base": _local_error(exc)},
            )

        return self._async_finish_reauth(entry, candidate)

    def _async_finish_reauth(
        self, entry: config_entries.ConfigEntry[Any], candidate: AupuConfigEntryData
    ) -> config_entries.ConfigFlowResult:
        """Finish either reauth method through HA's single atomic update path."""
        if entry.update_listeners:
            return self.async_update_and_abort(entry, data=candidate.as_mapping())
        return self.async_update_reload_and_abort(entry, data=candidate.as_mapping())

    def _create_entry(
        self, candidate: AupuConfigEntryData
    ) -> config_entries.ConfigFlowResult:
        """Persist only the validated JSON projection under a fixed title."""
        return self.async_create_entry(title=_ENTRY_TITLE, data=candidate.as_mapping())


class AupuOptionsFlow(config_entries.OptionsFlow):
    """Atomically replace local credential and connection options."""

    def __init__(self) -> None:
        self._pending: AupuConfigEntryData | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Validate a complete candidate before changing Config Entry data."""
        try:
            current = AupuConfigEntryData.from_mapping(
                self.config_entry.data, require_unexpired_token=False
            )
        except (AupuError, TypeError, ValueError):
            return self.async_abort(reason="invalid_entry")
        if user_input is None:
            return self.async_show_form(step_id="init", data_schema=_options_schema(current))

        try:
            submitted_token = user_input.get(_CONF_TOKEN, "")
            token = _parse_token(
                current.token if submitted_token == "" else submitted_token
            )
            phone = user_input.get(_CONF_PHONE, current.phone)
            use_wss = user_input.get(_CONF_USE_WSS, current.use_wss)
            candidate = AupuConfigEntryData.from_mapping(
                {
                    **current.as_mapping(),
                    _CONF_TOKEN: token,
                    _CONF_PHONE: phone,
                    _CONF_USE_WSS: use_wss,
                    _CONF_USER_UUID: current.user_uuid,
                },
                require_user_uuid=False,
            )
        except (AupuError, TypeError, ValueError) as exc:
            return self.async_show_form(
                step_id="init",
                data_schema=_options_schema(current),
                errors={"base": _local_error(exc)},
            )

        if candidate.use_wss:
            self._pending = candidate
            return self.async_show_form(
                step_id="confirm_wss", data_schema=_confirm_schema()
            )
        return await self._async_update_entry(candidate)

    async def async_step_confirm_wss(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Verify WSS identity before committing any options candidate."""
        if self._pending is None:
            return self.async_abort(reason="invalid_state")
        if user_input is None:
            return self.async_show_form(
                step_id="confirm_wss", data_schema=_confirm_schema()
            )
        try:
            user_uuid = await _async_verify_terminal_info(self.hass, self._pending)
            candidate = AupuConfigEntryData.from_mapping(
                {**self._pending.as_mapping(), _CONF_USER_UUID: user_uuid}
            )
        except (AupuError, TypeError, ValueError):
            return self.async_show_form(
                step_id="confirm_wss",
                data_schema=_confirm_schema(),
                errors={"base": "cannot_connect"},
            )
        return await self._async_update_entry(candidate)

    async def _async_update_entry(
        self, candidate: AupuConfigEntryData
    ) -> config_entries.ConfigFlowResult:
        """Apply one HA update only after the complete candidate is valid."""
        has_update_listener = bool(self.config_entry.update_listeners)
        updated = self.hass.config_entries.async_update_entry(
            self.config_entry, data=candidate.as_mapping()
        )
        if updated and not has_update_listener:
            await self.hass.config_entries.async_reload(self.config_entry.entry_id)
        return self.async_create_entry(title="", data={})
