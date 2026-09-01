"""Pure-local tests for the AUPU Q360 config entry lifecycle."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
from collections.abc import Coroutine, Generator, Mapping
from dataclasses import dataclass
from typing import Any, cast

import pytest
from homeassistant.config_entries import SOURCE_USER, ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.aupu_q360 import async_setup_entry, async_unload_entry
from custom_components.aupu_q360.config_flow import (
    AupuConfigFlow,
    AupuOptionsFlow,
)
from custom_components.aupu_q360.const import DOMAIN
from custom_components.aupu_q360.models import AupuRuntimeData
from custom_components.aupu_q360.signer import AppAuthorizationSigner

SYNTHETIC_SIGNER = {
    "app_key": "synthetic-app-key",
    "key_prefix": "synthetic-prefix",
    "package_name": "synthetic.package",
    "key_suffix": "synthetic-suffix",
    "sdk_version": "synthetic-sdk",
    "message_prefix": "synthetic-message",
    "sdk_label": "synthetic-sdk-label",
    "type_timestamp_label": "synthetic-timestamp-label",
    "header_prefix": "synthetic-header",
    "header_sep_1": "synthetic-separator-one",
    "header_sep_2": "synthetic-separator-two",
    "signature_label": "synthetic-signature-label",
}

_TEST_LOOP = asyncio.new_event_loop()


@pytest.fixture(scope="module", autouse=True)
def _close_test_loop() -> Generator[None]:
    """Close the pre-fixture loop used by pure direct-step calls."""
    yield
    _TEST_LOOP.close()


def _run[T](awaitable: Coroutine[Any, Any, T]) -> T:
    """Run one direct HA step without a Linux-only HA pytest fixture."""
    return _TEST_LOOP.run_until_complete(awaitable)


def make_synthetic_jwt(payload: object) -> str:
    """Encode a synthetic unsigned JWT-shaped value for local parsing tests."""

    def encode(value: object) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{encode({'alg': 'none'})}.{encode(payload)}.synthetic-signature"


def valid_token() -> str:
    """Return a locally unexpired synthetic token."""
    return make_synthetic_jwt({"exp": int(time.time()) + 7 * 24 * 60 * 60})


def user_input(**updates: object) -> dict[str, object]:
    """Build one valid user-step input with explicit synthetic data."""
    result: dict[str, object] = {
        "signer_json": json.dumps(SYNTHETIC_SIGNER),
        "token": valid_token(),
        "did": "123456789",
        "tag": "synthetic-tag",
        "use_wss": False,
    }
    result.update(updates)
    return result


@dataclass
class FakeEntry:
    """Minimal mutable config entry boundary used by pure unit tests."""

    data: Mapping[str, Any]
    unique_id: str | None = None
    entry_id: str = "synthetic-entry"
    domain: str = DOMAIN
    runtime_data: AupuRuntimeData | None = None


class FakeFlowManager:
    """Return no competing in-progress flows."""

    def async_progress_by_handler(
        self,
        handler: str,
        *,
        include_uninitialized: bool,
        match_context: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        del handler, include_uninitialized, match_context
        return []

    def async_abort(self, flow_id: str) -> None:
        del flow_id


class FakeConfigEntries:
    """Implement only the HA config-entry effects owned by this integration."""

    def __init__(self, entry: FakeEntry | None = None) -> None:
        self.entry = entry
        self.flow = FakeFlowManager()
        self.update_calls = 0
        self.forwarded: tuple[Platform, ...] | None = None
        self.unloaded: tuple[Platform, ...] | None = None
        self.unload_result = True

    def async_entry_for_domain_unique_id(
        self, domain: str, unique_id: str
    ) -> ConfigEntry[Any] | None:
        if (
            self.entry is not None
            and domain == DOMAIN
            and self.entry.unique_id == unique_id
        ):
            return cast(ConfigEntry[Any], self.entry)
        return None

    def async_entries(
        self, domain: str, include_ignore: bool = True
    ) -> list[ConfigEntry[Any]]:
        del include_ignore
        if self.entry is None or domain != DOMAIN:
            return []
        return [cast(ConfigEntry[Any], self.entry)]

    def async_get_known_entry(self, entry_id: str) -> ConfigEntry[Any]:
        assert self.entry is not None and self.entry.entry_id == entry_id
        return cast(ConfigEntry[Any], self.entry)

    def async_update_entry(
        self,
        entry: ConfigEntry[Any],
        *,
        data: Mapping[str, Any],
        **kwargs: Any,
    ) -> bool:
        del kwargs
        self.update_calls += 1
        cast(FakeEntry, entry).data = dict(data)
        return True

    async def async_forward_entry_setups(
        self, entry: ConfigEntry[Any], platforms: tuple[Platform, ...]
    ) -> None:
        del entry
        self.forwarded = platforms

    async def async_unload_platforms(
        self, entry: ConfigEntry[Any], platforms: tuple[Platform, ...]
    ) -> bool:
        del entry
        self.unloaded = platforms
        return self.unload_result


@dataclass
class FakeHass:
    """Minimal Home Assistant boundary for direct flow-step calls."""

    config_entries: FakeConfigEntries


def prepare_config_flow(entry: FakeEntry | None = None) -> tuple[AupuConfigFlow, FakeHass]:
    """Initialize the framework-owned attributes needed by a direct flow call."""
    hass = FakeHass(FakeConfigEntries(entry))
    flow = AupuConfigFlow()
    flow.hass = cast(HomeAssistant, hass)
    flow.handler = DOMAIN
    flow.flow_id = "synthetic-flow"
    flow.context = {"source": SOURCE_USER}
    return flow, hass


def prepare_options_flow(entry: FakeEntry) -> tuple[AupuOptionsFlow, FakeHass]:
    """Initialize the framework-owned attributes needed by an options call."""
    hass = FakeHass(FakeConfigEntries(entry))
    flow = AupuOptionsFlow()
    flow.hass = cast(HomeAssistant, hass)
    flow.handler = entry.entry_id
    flow.flow_id = "synthetic-options-flow"
    flow.context = {"source": "init"}
    return flow, hass


def test_user_step_defaults_to_https_only_without_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch a regression that contacts AUPU before explicit WSS confirmation."""
    calls = 0

    async def network_spy(*args: object, **kwargs: object) -> str:
        nonlocal calls
        del args, kwargs
        calls += 1
        return "must-not-be-used"

    monkeypatch.setattr(
        "custom_components.aupu_q360.config_flow._async_verify_terminal_info",
        network_spy,
    )
    flow, _ = prepare_config_flow()

    initial = _run(flow.async_step_user())
    assert initial["type"] is FlowResultType.FORM
    input_without_wss = user_input()
    del input_without_wss["use_wss"]
    schema = initial["data_schema"]
    assert schema is not None
    assert schema(input_without_wss)["use_wss"] is False

    result = _run(flow.async_step_user(user_input()))

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "AUPU Q360"
    assert result["data"] == {
        "signer": SYNTHETIC_SIGNER,
        "token": cast(str, user_input()["token"]),
        "did": "123456789",
        "tag": "synthetic-tag",
        "use_wss": False,
    }
    assert json.loads(json.dumps(result["data"])) == result["data"]
    assert calls == 0
    assert flow.unique_id == "15e2b0d3c33891ebb0f1"


@pytest.mark.parametrize(
    ("updates", "error"),
    [
        ({"signer_json": json.dumps({"app_key": "incomplete"})}, "invalid_signer"),
        ({"token": make_synthetic_jwt({"exp": 1})}, "expired_token"),
        ({"did": "not-digits"}, "invalid_device"),
        ({"tag": "   "}, "invalid_device"),
    ],
)
def test_user_step_rejects_invalid_local_input(
    updates: dict[str, object], error: str
) -> None:
    """Catch local validation branches accepting unusable persisted data."""
    flow, _ = prepare_config_flow()

    result = _run(flow.async_step_user(user_input(**updates)))

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": error}


def test_user_step_normalizes_one_bearer_prefix() -> None:
    """Catch persistence of multiple token representations."""
    token = valid_token()
    flow, _ = prepare_config_flow()

    result = _run(flow.async_step_user(user_input(token=f"Bearer {token}")))

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["token"] == token


def test_duplicate_device_aborts_by_hashed_unique_id() -> None:
    """Catch duplicate detection that stores or exposes the real device identifier."""
    expected_hash = hashlib.sha256(b"123456789").hexdigest()[:20]
    flow, _ = prepare_config_flow(FakeEntry(data={}, unique_id=expected_hash))

    result = _run(flow.async_step_user(user_input()))

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert flow.unique_id == expected_hash
    assert "123456789" not in repr(result)


def test_wss_requires_confirmation_then_verifies_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch WSS verification before consent or multiple read-only requests."""
    calls = 0

    async def verifier(*args: object, **kwargs: object) -> str:
        nonlocal calls
        del args, kwargs
        calls += 1
        return "synthetic-user-uuid"

    monkeypatch.setattr(
        "custom_components.aupu_q360.config_flow._async_verify_terminal_info",
        verifier,
    )
    flow, _ = prepare_config_flow()

    confirmation = _run(flow.async_step_user(user_input(use_wss=True)))

    assert confirmation["type"] is FlowResultType.FORM
    assert confirmation["step_id"] == "confirm_wss"
    assert calls == 0

    result = _run(flow.async_step_confirm_wss({}))

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["use_wss"] is True
    assert result["data"]["user_uuid"] == "synthetic-user-uuid"
    assert calls == 1


def test_wss_verification_failure_does_not_create_partial_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch failed read-only verification leaking a partially persisted entry."""

    async def verifier(*args: object, **kwargs: object) -> str:
        del args, kwargs
        raise ValueError("synthetic protocol failure")

    monkeypatch.setattr(
        "custom_components.aupu_q360.config_flow._async_verify_terminal_info",
        verifier,
    )
    flow, _ = prepare_config_flow()
    _run(flow.async_step_user(user_input(use_wss=True)))

    result = _run(flow.async_step_confirm_wss({}))

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm_wss"
    assert result["errors"] == {"base": "cannot_connect"}


def persisted_data(*, token: str | None = None, use_wss: bool = False) -> dict[str, object]:
    """Build a valid synthetic entry payload."""
    return {
        "signer": dict(SYNTHETIC_SIGNER),
        "token": token or valid_token(),
        "did": "123456789",
        "tag": "synthetic-tag",
        "use_wss": use_wss,
    }


def test_options_invalid_token_keeps_old_data_unchanged() -> None:
    """Catch validation failures that overwrite the last usable credential."""
    old_data = persisted_data()
    entry = FakeEntry(data=dict(old_data))
    flow, hass = prepare_options_flow(entry)

    result = _run(
        flow.async_step_init(
            {"token": make_synthetic_jwt({"exp": 1}), "phone": "", "use_wss": False}
        )
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "expired_token"}
    assert entry.data == old_data
    assert hass.config_entries.update_calls == 0


def test_options_can_replace_an_expired_stored_token() -> None:
    """Catch an expired old credential blocking the manual recovery path."""
    expired = make_synthetic_jwt({"exp": 1})
    old_data = persisted_data(token=expired)
    entry = FakeEntry(data=dict(old_data))
    flow, hass = prepare_options_flow(entry)
    replacement = valid_token()

    result = _run(
        flow.async_step_init(
            {"token": replacement, "phone": "", "use_wss": False}
        )
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.data == {**old_data, "token": replacement}
    assert hass.config_entries.update_calls == 1


def test_options_valid_token_and_phone_update_atomically() -> None:
    """Catch successful options that fail to normalize all persisted fields together."""
    old_data = persisted_data()
    entry = FakeEntry(data=dict(old_data))
    flow, hass = prepare_options_flow(entry)
    new_token = valid_token()

    result = _run(
        flow.async_step_init(
            {
                "token": f"Bearer {new_token}",
                "phone": " 13800000000 ",
                "use_wss": False,
            }
        )
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.data == {
        **old_data,
        "token": new_token,
        "phone": "13800000000",
    }
    assert hass.config_entries.update_calls == 1


def test_options_enabling_wss_waits_for_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch an options toggle that persists WSS before read-only verification succeeds."""
    old_data = persisted_data()
    entry = FakeEntry(data=dict(old_data))
    flow, hass = prepare_options_flow(entry)
    calls = 0

    async def verifier(*args: object, **kwargs: object) -> str:
        nonlocal calls
        del args, kwargs
        calls += 1
        return "synthetic-user-uuid"

    monkeypatch.setattr(
        "custom_components.aupu_q360.config_flow._async_verify_terminal_info",
        verifier,
    )

    confirmation = _run(
        flow.async_step_init({"token": valid_token(), "phone": "", "use_wss": True})
    )

    assert confirmation["type"] is FlowResultType.FORM
    assert confirmation["step_id"] == "confirm_wss"
    assert entry.data == old_data
    assert hass.config_entries.update_calls == 0
    assert calls == 0

    result = _run(flow.async_step_confirm_wss({}))

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.data["use_wss"] is True
    assert entry.data["user_uuid"] == "synthetic-user-uuid"
    assert hass.config_entries.update_calls == 1
    assert calls == 1


def test_setup_builds_runtime_and_forwards_light_without_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch setup that persists raw objects or omits a required runtime dependency."""
    entry = FakeEntry(data=persisted_data())
    hass = FakeHass(FakeConfigEntries(entry))
    session = object()
    monkeypatch.setattr(
        "custom_components.aupu_q360.async_get_clientsession",
        lambda _: session,
    )

    result = _run(
        async_setup_entry(
            cast(HomeAssistant, hass), cast(ConfigEntry[AupuRuntimeData], entry)
        )
    )

    assert result is True
    assert isinstance(entry.runtime_data, AupuRuntimeData)
    assert isinstance(entry.runtime_data.signer, AppAuthorizationSigner)
    assert entry.runtime_data.device.did == "123456789"
    assert entry.runtime_data.credential.authorization_header.startswith("Bearer ")
    assert hass.config_entries.forwarded == (Platform.LIGHT,)


class Stopper:
    """Record one safe asynchronous runtime stop."""

    def __init__(self) -> None:
        self.calls = 0

    async def async_stop(self) -> None:
        self.calls += 1


def test_successful_unload_stops_runtime_and_clears_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch leaked background objects after a successful platform unload."""
    entry = FakeEntry(data=persisted_data())
    hass = FakeHass(FakeConfigEntries(entry))
    monkeypatch.setattr(
        "custom_components.aupu_q360.async_get_clientsession",
        lambda _: object(),
    )
    _run(
        async_setup_entry(
            cast(HomeAssistant, hass), cast(ConfigEntry[AupuRuntimeData], entry)
        )
    )
    assert entry.runtime_data is not None
    stopper = Stopper()
    entry.runtime_data.stoppers.append(stopper)

    result = _run(
        async_unload_entry(
            cast(HomeAssistant, hass), cast(ConfigEntry[AupuRuntimeData], entry)
        )
    )

    assert result is True
    assert stopper.calls == 1
    assert hass.config_entries.unloaded == (Platform.LIGHT,)
    assert "runtime_data" not in entry.__dict__


def test_failed_platform_unload_keeps_runtime_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch partial teardown when Home Assistant rejects platform unload."""
    entry = FakeEntry(data=persisted_data())
    hass = FakeHass(FakeConfigEntries(entry))
    hass.config_entries.unload_result = False
    monkeypatch.setattr(
        "custom_components.aupu_q360.async_get_clientsession",
        lambda _: object(),
    )
    _run(
        async_setup_entry(
            cast(HomeAssistant, hass), cast(ConfigEntry[AupuRuntimeData], entry)
        )
    )
    assert entry.runtime_data is not None
    stopper = Stopper()
    entry.runtime_data.stoppers.append(stopper)

    result = _run(
        async_unload_entry(
            cast(HomeAssistant, hass), cast(ConfigEntry[AupuRuntimeData], entry)
        )
    )

    assert result is False
    assert stopper.calls == 0
    assert entry.runtime_data is not None
