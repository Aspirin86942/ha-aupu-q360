"""Pure-local tests for the AUPU Q360 config entry lifecycle."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import time
from collections.abc import Callable, Coroutine, Generator, Mapping
from dataclasses import dataclass, field
from typing import Any, cast

import pytest
from homeassistant.config_entries import SOURCE_USER, ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.aupu_q360 import (
    _async_teardown_runtime,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.aupu_q360.config_flow import (
    AupuConfigFlow,
    AupuOptionsFlow,
)
from custom_components.aupu_q360.const import DOMAIN
from custom_components.aupu_q360.models import ApiResponse, AupuRuntimeData
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
    update_listeners: list[Callable[[HomeAssistant, ConfigEntry[Any]], Coroutine[Any, Any, None]]] = field(
        default_factory=list
    )
    unload_callbacks: list[Callable[[], Coroutine[Any, Any, None] | None]] = field(
        default_factory=list
    )

    def add_update_listener(
        self,
        listener: Callable[[HomeAssistant, ConfigEntry[Any]], Coroutine[Any, Any, None]],
    ) -> Callable[[], None]:
        """Register the same listener boundary used by a real ConfigEntry."""
        self.update_listeners.append(listener)

        def unsubscribe() -> None:
            self.update_listeners.remove(listener)

        return unsubscribe

    def async_on_unload(
        self, callback: Callable[[], Coroutine[Any, Any, None] | None]
    ) -> None:
        """Keep listener cleanup until the fake manager completes unload."""
        self.unload_callbacks.append(callback)

    async def async_process_on_unload(self) -> None:
        """Mirror HA's LIFO unload callback processing for listener cleanup."""
        while self.unload_callbacks:
            result = self.unload_callbacks.pop()()
            if result is not None:
                await result


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
        self.hass: HomeAssistant | None = None
        self.flow = FakeFlowManager()
        self.update_calls = 0
        self.reload_calls = 0
        self.listener_tasks: list[asyncio.Task[None]] = []
        self.forwarded: tuple[Platform, ...] | None = None
        self.forward_error: BaseException | None = None
        self.forward_stoppers: list[Any] = []
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
        if entry.data == data:
            return False
        self.update_calls += 1
        fake_entry = cast(FakeEntry, entry)
        fake_entry.data = dict(data)
        assert self.hass is not None
        for listener in fake_entry.update_listeners:
            self.listener_tasks.append(
                asyncio.create_task(listener(self.hass, entry))
            )
        return True

    async def async_wait_for_update_listeners(self) -> None:
        """Wait for all listener work scheduled by an entry update."""
        if self.listener_tasks:
            await asyncio.gather(*self.listener_tasks)
            self.listener_tasks.clear()

    async def async_reload(self, entry_id: str) -> bool:
        """Mirror the integration-owned effects of one HA reload."""
        assert self.entry is not None and self.entry.entry_id == entry_id
        assert self.hass is not None
        self.reload_calls += 1
        entry = cast(ConfigEntry[AupuRuntimeData], self.entry)
        if not await async_unload_entry(self.hass, entry):
            return False
        await self.entry.async_process_on_unload()
        return await async_setup_entry(self.hass, entry)

    async def async_forward_entry_setups(
        self, entry: ConfigEntry[Any], platforms: tuple[Platform, ...]
    ) -> None:
        self.forwarded = platforms
        runtime = cast(FakeEntry, entry).runtime_data
        assert runtime is not None
        runtime.stoppers.extend(self.forward_stoppers)
        if self.forward_error is not None:
            raise self.forward_error

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

    def __post_init__(self) -> None:
        self.config_entries.hass = cast(HomeAssistant, self)


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
    """Catch wrong terminal-info method/path or verification before consent."""
    session_sentinel = object()
    requests: list[tuple[str, str, Mapping[str, Any]]] = []

    class FakeApiClient:
        """Replace only the external client while running the real verifier."""

        def __init__(self, *, session: object, **kwargs: object) -> None:
            del kwargs
            assert session is session_sentinel

        async def request(
            self, method: str, path: str, *, json: Mapping[str, Any]
        ) -> ApiResponse:
            requests.append((method, path, json))
            return ApiResponse(
                status=0,
                result={
                    "content": {
                        "userUuid": "synthetic-user-uuid",
                        "ignored": "not-persisted",
                    },
                    "ignored": "not-persisted",
                },
                timestamp=1,
            )

    monkeypatch.setattr(
        "custom_components.aupu_q360.config_flow.async_get_clientsession",
        lambda _: session_sentinel,
    )
    monkeypatch.setattr(
        "custom_components.aupu_q360.config_flow.AupuApiClient", FakeApiClient
    )
    flow, _ = prepare_config_flow()

    confirmation = _run(flow.async_step_user(user_input(use_wss=True)))

    assert confirmation["type"] is FlowResultType.FORM
    assert confirmation["step_id"] == "confirm_wss"
    assert requests == []

    result = _run(flow.async_step_confirm_wss({}))

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["use_wss"] is True
    assert result["data"]["user_uuid"] == "synthetic-user-uuid"
    assert "not-persisted" not in repr(result["data"])
    assert requests == [
        ("GET", "/authserver/auth/user/terminal/info", {})
    ]


def test_wss_verification_failure_does_not_create_partial_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch failed read-only verification leaking a partially persisted entry."""
    requests: list[tuple[str, str, Mapping[str, Any]]] = []

    class FakeApiClient:
        """Return a complete response envelope with an unusable user UUID."""

        def __init__(self, **kwargs: object) -> None:
            del kwargs

        async def request(
            self, method: str, path: str, *, json: Mapping[str, Any]
        ) -> ApiResponse:
            requests.append((method, path, json))
            return ApiResponse(
                status=0,
                result={"content": {"userUuid": ""}},
                timestamp=1,
            )

    monkeypatch.setattr(
        "custom_components.aupu_q360.config_flow.async_get_clientsession",
        lambda _: object(),
    )
    monkeypatch.setattr(
        "custom_components.aupu_q360.config_flow.AupuApiClient", FakeApiClient
    )
    flow, hass = prepare_config_flow()
    _run(flow.async_step_user(user_input(use_wss=True)))

    result = _run(flow.async_step_confirm_wss({}))

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm_wss"
    assert result["errors"] == {"base": "cannot_connect"}
    assert requests == [("GET", "/authserver/auth/user/terminal/info", {})]
    assert hass.config_entries.update_calls == 0


def persisted_data(*, token: str | None = None, use_wss: bool = False) -> dict[str, object]:
    """Build a valid synthetic entry payload."""
    return {
        "signer": dict(SYNTHETIC_SIGNER),
        "token": token or valid_token(),
        "did": "123456789",
        "tag": "synthetic-tag",
        "use_wss": use_wss,
    }


def test_options_invalid_token_keeps_loaded_runtime_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch invalid options writing data, reloading, or replacing loaded runtime."""
    old_data = persisted_data()
    entry = FakeEntry(data=dict(old_data))
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
    old_runtime = entry.runtime_data
    assert old_runtime is not None
    flow = AupuOptionsFlow()
    flow.hass = cast(HomeAssistant, hass)
    flow.handler = entry.entry_id
    flow.flow_id = "invalid-loaded-options-flow"
    flow.context = {"source": "init"}

    result = _run(
        flow.async_step_init(
            {"token": make_synthetic_jwt({"exp": 1}), "phone": "", "use_wss": False}
        )
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "expired_token"}
    assert entry.data == old_data
    assert hass.config_entries.update_calls == 0
    assert hass.config_entries.reload_calls == 0
    assert entry.runtime_data is old_runtime


def test_options_can_replace_an_expired_stored_token() -> None:
    """Catch an expired old credential blocking the manual recovery path."""
    expired = make_synthetic_jwt({"exp": 1})
    old_data = persisted_data(token=expired)
    entry = FakeEntry(data=dict(old_data))
    flow, hass = prepare_options_flow(entry)
    replacement = make_synthetic_jwt(
        {"exp": int(time.time()) + 7 * 24 * 60 * 60, "sub": "replacement"}
    )

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


def test_loaded_options_update_reloads_runtime_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch entry updates leaving the loaded runtime on the old credential."""
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
    old_runtime = entry.runtime_data
    assert old_runtime is not None
    replacement = make_synthetic_jwt(
        {"exp": int(time.time()) + 7 * 24 * 60 * 60, "sub": "runtime-replacement"}
    )
    flow = AupuOptionsFlow()
    flow.hass = cast(HomeAssistant, hass)
    flow.handler = entry.entry_id
    flow.flow_id = "loaded-options-flow"
    flow.context = {"source": "init"}

    result = _run(
        flow.async_step_init(
            {"token": replacement, "phone": "", "use_wss": False}
        )
    )
    _run(hass.config_entries.async_wait_for_update_listeners())

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert hass.config_entries.update_calls == 1
    assert hass.config_entries.reload_calls == 1
    assert entry.runtime_data is not None
    assert entry.runtime_data is not old_runtime
    assert entry.runtime_data.credential.authorization_header == f"Bearer {replacement}"
    assert len(entry.update_listeners) == 1
    assert len(entry.unload_callbacks) == 1


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

    def __init__(self, failure: BaseException | None = None) -> None:
        self.calls = 0
        self.failure = failure

    async def async_stop(self) -> None:
        self.calls += 1
        if self.failure is not None:
            raise self.failure


class BlockingStopper:
    """Expose when runtime teardown is suspended inside one stopper."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.calls = 0

    async def async_stop(self) -> None:
        self.calls += 1
        self.started.set()
        await asyncio.Event().wait()


def test_forward_failure_stops_all_unique_stoppers_and_preserves_primary_error(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Catch setup failures leaking tasks or cleanup masking the forward error."""
    entry = FakeEntry(data=persisted_data())
    hass = FakeHass(FakeConfigEntries(entry))
    primary_error = RuntimeError("synthetic forward failure")
    failing_stopper = Stopper(RuntimeError("private stopper detail"))
    remaining_stopper = Stopper()
    hass.config_entries.forward_stoppers = [
        failing_stopper,
        failing_stopper,
        remaining_stopper,
    ]
    hass.config_entries.forward_error = primary_error
    monkeypatch.setattr(
        "custom_components.aupu_q360.async_get_clientsession",
        lambda _: object(),
    )

    with (
        caplog.at_level(logging.ERROR, logger="custom_components.aupu_q360"),
        pytest.raises(RuntimeError, match="synthetic forward failure") as raised,
    ):
        _run(
            async_setup_entry(
                cast(HomeAssistant, hass),
                cast(ConfigEntry[AupuRuntimeData], entry),
            )
        )

    assert raised.value is primary_error
    assert failing_stopper.calls == 1
    assert remaining_stopper.calls == 1
    assert "runtime_data" not in entry.__dict__
    assert "AUPU runtime teardown failed" in caplog.text
    assert "private stopper detail" not in caplog.text


def test_forward_failure_survives_cancelled_stopper(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Catch stopper cancellation masking the primary setup failure."""
    entry = FakeEntry(data=persisted_data())
    hass = FakeHass(FakeConfigEntries(entry))
    primary_error = RuntimeError("synthetic forward failure")
    cancelled_stopper = Stopper(
        asyncio.CancelledError("private cancellation detail")
    )
    remaining_stopper = Stopper()
    hass.config_entries.forward_stoppers = [cancelled_stopper, remaining_stopper]
    hass.config_entries.forward_error = primary_error
    monkeypatch.setattr(
        "custom_components.aupu_q360.async_get_clientsession",
        lambda _: object(),
    )

    with (
        caplog.at_level(logging.ERROR, logger="custom_components.aupu_q360"),
        pytest.raises(RuntimeError, match="synthetic forward failure") as raised,
    ):
        _run(
            async_setup_entry(
                cast(HomeAssistant, hass),
                cast(ConfigEntry[AupuRuntimeData], entry),
            )
        )

    assert raised.value is primary_error
    assert cancelled_stopper.calls == 1
    assert remaining_stopper.calls == 1
    assert "runtime_data" not in entry.__dict__
    assert "AUPU runtime teardown failed" in caplog.text
    assert "private cancellation detail" not in caplog.text


def test_forward_failure_cleanup_propagates_external_cancel(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Catch external cancellation being replaced by the forward failure."""
    entry = FakeEntry(data=persisted_data())
    hass = FakeHass(FakeConfigEntries(entry))
    blocking_stopper = BlockingStopper()
    remaining_stopper = Stopper()
    hass.config_entries.forward_stoppers = [blocking_stopper, remaining_stopper]
    hass.config_entries.forward_error = RuntimeError("synthetic forward failure")
    monkeypatch.setattr(
        "custom_components.aupu_q360.async_get_clientsession",
        lambda _: object(),
    )

    async def cancel_during_cleanup() -> None:
        task = asyncio.create_task(
            async_setup_entry(
                cast(HomeAssistant, hass),
                cast(ConfigEntry[AupuRuntimeData], entry),
            )
        )
        await blocking_stopper.started.wait()
        task.cancel("private external cancellation detail")
        with pytest.raises(asyncio.CancelledError):
            await task

    with caplog.at_level(logging.ERROR, logger="custom_components.aupu_q360"):
        _run(cancel_during_cleanup())

    assert blocking_stopper.calls == 1
    assert remaining_stopper.calls == 1
    assert "runtime_data" not in entry.__dict__
    assert "AUPU runtime teardown failed" not in caplog.text
    assert "private external cancellation detail" not in caplog.text


def test_successful_unload_continues_after_cancelled_stopper(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Catch one cancelled stopper leaving later runtime resources active."""
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
    cancelled_stopper = Stopper(
        asyncio.CancelledError("private cancellation detail")
    )
    remaining_stopper = Stopper()
    entry.runtime_data.stoppers.extend([cancelled_stopper, remaining_stopper])

    with caplog.at_level(logging.ERROR, logger="custom_components.aupu_q360"):
        result = _run(
            async_unload_entry(
                cast(HomeAssistant, hass),
                cast(ConfigEntry[AupuRuntimeData], entry),
            )
        )

    assert result is True
    assert cancelled_stopper.calls == 1
    assert remaining_stopper.calls == 1
    assert "runtime_data" not in entry.__dict__
    assert "AUPU runtime teardown failed" in caplog.text
    assert "private cancellation detail" not in caplog.text


def test_successful_unload_propagates_external_cancel(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Catch unload returning success after its task was externally cancelled."""
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
    blocking_stopper = BlockingStopper()
    remaining_stopper = Stopper()
    entry.runtime_data.stoppers.extend([blocking_stopper, remaining_stopper])

    async def cancel_during_unload() -> None:
        task = asyncio.create_task(
            async_unload_entry(
                cast(HomeAssistant, hass),
                cast(ConfigEntry[AupuRuntimeData], entry),
            )
        )
        await blocking_stopper.started.wait()
        task.cancel("private external cancellation detail")
        with pytest.raises(asyncio.CancelledError):
            await task

    with caplog.at_level(logging.ERROR, logger="custom_components.aupu_q360"):
        _run(cancel_during_unload())

    assert blocking_stopper.calls == 1
    assert remaining_stopper.calls == 1
    assert "runtime_data" not in entry.__dict__
    assert "AUPU runtime teardown failed" not in caplog.text
    assert "private external cancellation detail" not in caplog.text


@pytest.mark.parametrize("control_error_type", [KeyboardInterrupt, SystemExit])
def test_teardown_propagates_control_base_exception(
    control_error_type: type[BaseException],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch process-control exceptions being demoted to teardown failures."""
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
    entry.runtime_data.stoppers.append(
        Stopper(control_error_type("synthetic control exception"))
    )

    async def assert_control_exception_propagates() -> None:
        with pytest.raises(control_error_type):
            await _async_teardown_runtime(
                cast(ConfigEntry[AupuRuntimeData], entry)
            )

    _run(assert_control_exception_propagates())

    assert "runtime_data" not in entry.__dict__


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
    _run(entry.async_process_on_unload())
    assert entry.update_listeners == []
    assert entry.unload_callbacks == []


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
