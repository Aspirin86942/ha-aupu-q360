"""Core behavior tests for the Q360 light coordinator and sole entity."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Callable, Coroutine, Generator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from homeassistant.components.light.const import ColorMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.issue_registry import IssueSeverity

from custom_components.aupu_q360 import async_setup_entry, async_unload_entry
from custom_components.aupu_q360.api import AupuApiClient
from custom_components.aupu_q360.auth import BearerCredential
from custom_components.aupu_q360.const import DOMAIN
from custom_components.aupu_q360.coordinator import AupuCoordinator
from custom_components.aupu_q360.errors import AupuAuthError, AupuTemporaryError
from custom_components.aupu_q360.light import AupuLight
from custom_components.aupu_q360.light import async_setup_entry as async_setup_light
from custom_components.aupu_q360.models import ApiResponse, AupuRuntimeData
from custom_components.aupu_q360.shadow import LightShadowUpdate
from custom_components.aupu_q360.wss import AupuShadowWebSocket

_TEST_LOOP = asyncio.new_event_loop()


@pytest.fixture(scope="module", autouse=True)
def _close_test_loop() -> Generator[None]:
    yield
    _TEST_LOOP.close()


def _run[T](awaitable: Coroutine[Any, Any, T]) -> T:
    return _TEST_LOOP.run_until_complete(awaitable)


def _token(expires_at: datetime) -> str:
    payload = json.dumps({"exp": int(expires_at.timestamp())}).encode()
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return f"e30.{encoded}.signature"


def _credential(offset: timedelta) -> BearerCredential:
    return BearerCredential.parse(_token(datetime.now(UTC) + offset))


class FakeApi:
    """Keep the external HTTPS boundary fake while running coordinator code."""

    def __init__(self) -> None:
        self.calls: list[bool] = []
        self.failure: Exception | None = None

    async def set_light(self, is_on: bool) -> ApiResponse:
        self.calls.append(is_on)
        if self.failure is not None:
            raise self.failure
        return ApiResponse(status=0, result={"accepted": True}, timestamp=1)


@dataclass
class IssueRecorder:
    created: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    deleted: list[tuple[str, str]] = field(default_factory=list)

    def create(
        self,
        hass: HomeAssistant,
        domain: str,
        issue_id: str,
        **kwargs: Any,
    ) -> None:
        del hass
        self.created.append((domain, issue_id, kwargs))

    def delete(self, hass: HomeAssistant, domain: str, issue_id: str) -> None:
        del hass
        self.deleted.append((domain, issue_id))


@pytest.fixture
def issues(monkeypatch: pytest.MonkeyPatch) -> IssueRecorder:
    recorder = IssueRecorder()
    monkeypatch.setattr(
        "custom_components.aupu_q360.coordinator.ir.async_create_issue",
        recorder.create,
    )
    monkeypatch.setattr(
        "custom_components.aupu_q360.coordinator.ir.async_delete_issue",
        recorder.delete,
    )
    return recorder


def _coordinator(
    api: FakeApi,
    credential: BearerCredential | None = None,
    *,
    entry_id: str = "synthetic-entry",
    reauth_requests: list[None] | None = None,
) -> AupuCoordinator:
    hass = type("FakeRepairHass", (), {"data": {}})()

    def request_reauth() -> None:
        if reauth_requests is not None:
            reauth_requests.append(None)

    return AupuCoordinator(
        hass=cast(HomeAssistant, hass),
        entry_id=entry_id,
        credential=credential or _credential(timedelta(days=2)),
        api=cast(AupuApiClient, api),
        async_request_reauth=request_reauth,
    )


def test_entity_turn_on_and_off_send_one_boolean_and_publish_assumed_state(
    issues: IssueRecorder,
) -> None:
    """Catch wrong booleans, replayed calls, or missing optimistic state."""
    del issues
    api = FakeApi()
    coordinator = _coordinator(api)
    entity = AupuLight(
        coordinator=coordinator,
        entry_id="synthetic-entry",
        unique_id="9f4e70f00edb76c1b3d8",
    )

    _run(entity.async_turn_on())

    assert api.calls == [True]
    assert entity.is_on is True
    assert entity.assumed_state is True

    _run(entity.async_turn_off())

    assert api.calls == [True, False]
    assert entity.is_on is False
    assert entity.assumed_state is True


def test_api_failure_is_visible_and_keeps_previous_entity_state(
    issues: IssueRecorder,
) -> None:
    """Catch a failed control being presented as successful or silently swallowed."""
    del issues
    api = FakeApi()
    coordinator = _coordinator(api)
    entity = AupuLight(
        coordinator=coordinator,
        entry_id="synthetic-entry",
        unique_id="9f4e70f00edb76c1b3d8",
    )
    _run(entity.async_turn_on())
    api.failure = AupuTemporaryError()

    with pytest.raises(HomeAssistantError, match="Light control failed"):
        _run(entity.async_turn_off())

    assert api.calls == [True, False]
    assert entity.is_on is True
    assert entity.assumed_state is True


def test_expired_credential_blocks_api_and_creates_entry_scoped_repair(
    issues: IssueRecorder,
) -> None:
    """Catch an expired JWT reaching the transport or colliding across entries."""
    api = FakeApi()
    reauth_requests: list[None] = []
    coordinator = _coordinator(
        api,
        _credential(timedelta(hours=-1)),
        entry_id="entry-b",
        reauth_requests=reauth_requests,
    )

    with pytest.raises(ConfigEntryAuthFailed):
        _run(coordinator.async_set_light(True))

    assert api.calls == []
    assert issues.deleted == [(DOMAIN, "entry-b_jwt_expiring")]
    assert issues.created == [
        (
            DOMAIN,
            "entry-b_jwt_expired",
            {
                "is_fixable": False,
                "is_persistent": True,
                "severity": IssueSeverity.ERROR,
                "translation_key": "jwt_expired",
            },
        )
    ]

    with pytest.raises(ConfigEntryAuthFailed):
        _run(coordinator.async_set_light(False))

    assert api.calls == []
    assert reauth_requests == [None]


def test_remote_auth_failure_triggers_reauth_once_without_changing_state(
    issues: IssueRecorder,
) -> None:
    """Catch remote auth errors being retried or changing the optimistic state."""
    del issues
    api = FakeApi()
    reauth_requests: list[None] = []
    coordinator = _coordinator(api, reauth_requests=reauth_requests)
    coordinator.async_apply_light_state(is_on=False, confirmed=True)
    api.failure = AupuAuthError()

    with pytest.raises(ConfigEntryAuthFailed):
        _run(coordinator.async_set_light(True))

    with pytest.raises(ConfigEntryAuthFailed):
        _run(coordinator.async_set_light(True))

    assert api.calls == [True, True]
    assert reauth_requests == [None]
    assert coordinator.is_on is False
    assert coordinator.assumed_state is False


class RecordingLight(AupuLight):
    """Record entity notifications without requiring HA's unavailable test fixture."""

    def __init__(self, coordinator: AupuCoordinator) -> None:
        super().__init__(
            coordinator=coordinator,
            entry_id="synthetic-entry",
            unique_id="9f4e70f00edb76c1b3d8",
        )
        self.writes = 0

    def async_write_ha_state(self) -> None:
        self.writes += 1


def test_confirmed_state_notifies_entity_and_removal_unsubscribes(
    issues: IssueRecorder,
) -> None:
    """Catch confirmed Shadow state retaining assumed state or leaking listeners."""
    del issues
    coordinator = _coordinator(FakeApi())
    entity = RecordingLight(coordinator)
    _run(entity.async_added_to_hass())

    coordinator.async_apply_light_state(is_on=True, confirmed=False)
    assert entity.is_on is True
    assert entity.assumed_state is True
    assert entity.writes == 1

    coordinator.async_apply_light_state(is_on=False, confirmed=True)
    assert entity.is_on is False
    assert entity.assumed_state is False
    assert entity.writes == 2

    _run(entity.async_will_remove_from_hass())
    coordinator.async_apply_light_state(is_on=True, confirmed=True)
    assert entity.writes == 2


def test_shadow_source_and_disconnect_preserve_confirmed_state(
    issues: IssueRecorder,
) -> None:
    """Catch desired/disconnect updates masquerading as physical confirmation."""
    del issues
    coordinator = _coordinator(FakeApi())
    entity = AupuLight(
        coordinator=coordinator,
        entry_id="synthetic-entry",
        unique_id="synthetic-light",
    )

    coordinator.async_apply_shadow_update(
        LightShadowUpdate(is_on=True, confirmed=False, source="desired")
    )
    assert coordinator.is_on is True
    assert coordinator.assumed_state is True

    coordinator.async_apply_wss_connection(connected=True, healthy=True)
    assert coordinator.wss_connected is True
    assert coordinator.wss_healthy is True
    assert entity.extra_state_attributes == {
        "wss_connected": True,
        "wss_healthy": True,
    }

    coordinator.async_apply_shadow_update(
        LightShadowUpdate(is_on=False, confirmed=True, source="reported")
    )
    coordinator.async_apply_wss_connection(connected=False, healthy=False)

    assert coordinator.is_on is False
    assert coordinator.assumed_state is False
    assert coordinator.wss_connected is False
    assert coordinator.wss_healthy is False
    assert entity.extra_state_attributes == {
        "wss_connected": False,
        "wss_healthy": False,
    }


def test_wss_auth_failure_reuses_deduplicated_reauth_entry(
    issues: IssueRecorder,
) -> None:
    """Catch repeated WSS authentication callbacks creating duplicate Reauth flows."""
    del issues
    requests: list[None] = []
    coordinator = _coordinator(FakeApi(), reauth_requests=requests)

    coordinator.async_handle_wss_auth_failure()
    coordinator.async_handle_wss_auth_failure()

    assert requests == [None]


@pytest.mark.parametrize(
    ("offset", "created_id", "severity", "persistent", "raises_auth"),
    [
        (timedelta(days=2), None, None, None, False),
        (timedelta(hours=1), "entry-a_jwt_expiring", IssueSeverity.WARNING, False, False),
        (timedelta(hours=-1), "entry-a_jwt_expired", IssueSeverity.ERROR, True, True),
    ],
)
def test_start_reconciles_repairs_for_each_jwt_state(
    issues: IssueRecorder,
    offset: timedelta,
    created_id: str | None,
    severity: IssueSeverity | None,
    persistent: bool | None,
    raises_auth: bool,
) -> None:
    """Catch reload leaving stale issues or creating the wrong Repair severity."""
    coordinator = _coordinator(
        FakeApi(),
        _credential(offset),
        entry_id="entry-a",
    )

    if raises_auth:
        with pytest.raises(ConfigEntryAuthFailed):
            _run(coordinator.async_start())
    else:
        _run(coordinator.async_start())

    if created_id is None:
        assert issues.created == []
        assert issues.deleted == [
            (DOMAIN, "entry-a_jwt_expiring"),
            (DOMAIN, "entry-a_jwt_expired"),
        ]
        return

    assert len(issues.created) == 1
    domain, issue_id, kwargs = issues.created[0]
    assert (domain, issue_id) == (DOMAIN, created_id)
    assert kwargs == {
        "is_fixable": False,
        "is_persistent": persistent,
        "severity": severity,
        "translation_key": created_id.removeprefix("entry-a_"),
    }
    stale_id = (
        "entry-a_jwt_expired"
        if created_id.endswith("jwt_expiring")
        else "entry-a_jwt_expiring"
    )
    assert issues.deleted == [(DOMAIN, stale_id)]


@dataclass
class FakeEntry:
    data: Mapping[str, Any]
    unique_id: str | None = "9f4e70f00edb76c1b3d8"
    entry_id: str = "synthetic-entry"
    runtime_data: AupuRuntimeData | None = None
    unload_callbacks: list[Callable[[], Coroutine[Any, Any, None] | None]] = field(
        default_factory=list
    )
    reauth_calls: int = 0

    def async_start_reauth(self, hass: HomeAssistant) -> None:
        del hass
        self.reauth_calls += 1

    def add_update_listener(
        self,
        listener: Callable[[HomeAssistant, ConfigEntry[Any]], Coroutine[Any, Any, None]],
    ) -> Callable[[], None]:
        del listener
        return lambda: None

    def async_on_unload(
        self,
        callback: Callable[[], Coroutine[Any, Any, None] | None],
    ) -> None:
        self.unload_callbacks.append(callback)


class FakeConfigEntries:
    def __init__(self) -> None:
        self.hass: FakeHass
        self.entities: list[AupuLight] = []
        self.forwarded: tuple[Platform, ...] | None = None
        self.unloaded: tuple[Platform, ...] | None = None

    async def async_forward_entry_setups(
        self,
        entry: ConfigEntry[Any],
        platforms: tuple[Platform, ...],
    ) -> None:
        self.forwarded = platforms

        def add_entities(entities: list[AupuLight]) -> None:
            self.entities.extend(entities)

        await async_setup_light(
            cast(HomeAssistant, self.hass),
            cast(ConfigEntry[AupuRuntimeData], entry),
            cast(Any, add_entities),
        )
        for entity in self.entities:
            await entity.async_added_to_hass()

    async def async_unload_platforms(
        self,
        entry: ConfigEntry[Any],
        platforms: tuple[Platform, ...],
    ) -> bool:
        del entry
        self.unloaded = platforms
        for entity in self.entities:
            await entity.async_will_remove_from_hass()
        return True

    async def async_reload(self, entry_id: str) -> bool:
        del entry_id
        return True


@dataclass
class FakeHass:
    config_entries: FakeConfigEntries
    data: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.config_entries.hass = self


def _persisted_data(
    *,
    token: str | None = None,
    use_wss: bool = False,
    user_uuid: str | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "signer": {
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
        },
        "token": token or _token(datetime.now(UTC) + timedelta(days=2)),
        "did": "123456789",
        "tag": "synthetic-tag",
        "use_wss": use_wss,
    }
    if user_uuid is not None:
        result["user_uuid"] = user_uuid
    return result


def test_setup_starts_one_coordinator_stopper_and_adds_only_one_light(
    monkeypatch: pytest.MonkeyPatch,
    issues: IssueRecorder,
) -> None:
    """Catch extra platforms/entities, raw IDs, or coordinator teardown leaks."""
    del issues
    entry = FakeEntry(data=_persisted_data())
    config_entries = FakeConfigEntries()
    hass = FakeHass(config_entries)
    stop_calls = 0
    original_stop = AupuCoordinator.async_stop

    async def record_stop(coordinator: AupuCoordinator) -> None:
        nonlocal stop_calls
        stop_calls += 1
        await original_stop(coordinator)

    monkeypatch.setattr(AupuCoordinator, "async_stop", record_stop)
    monkeypatch.setattr(
        "custom_components.aupu_q360.async_get_clientsession",
        lambda _: object(),
    )

    assert _run(
        async_setup_entry(
            cast(HomeAssistant, hass),
            cast(ConfigEntry[AupuRuntimeData], entry),
        )
    ) is True
    assert entry.runtime_data is not None
    coordinator = entry.runtime_data.coordinator
    assert entry.runtime_data.stoppers == [coordinator]
    assert config_entries.forwarded == (Platform.LIGHT,)
    assert len(config_entries.entities) == 1
    entity = config_entries.entities[0]
    assert entity.name == "Q360T5-Pro Light"
    assert entity.unique_id == "9f4e70f00edb76c1b3d8"
    assert entity.supported_color_modes == {ColorMode.ONOFF}
    assert entity.device_info == {
        "identifiers": {(DOMAIN, "synthetic-entry")},
        "manufacturer": "AUPU",
        "model": "Q360T5-Pro",
        "name": "AUPU Q360T5-Pro",
    }
    assert "123456789" not in repr(entity.device_info)
    assert "synthetic-tag" not in repr(entity.device_info)

    assert _run(
        async_unload_entry(
            cast(HomeAssistant, hass),
            cast(ConfigEntry[AupuRuntimeData], entry),
        )
    ) is True
    assert config_entries.unloaded == (Platform.LIGHT,)
    assert stop_calls == 1
    assert "runtime_data" not in entry.__dict__


def test_expired_setup_reconciles_repair_then_fails_auth_without_forwarding(
    monkeypatch: pytest.MonkeyPatch,
    issues: IssueRecorder,
) -> None:
    """Catch setup rejecting expired data before Repair or exposing a dead entity."""
    entry = FakeEntry(
        data=_persisted_data(token=_token(datetime.now(UTC) - timedelta(hours=1)))
    )
    config_entries = FakeConfigEntries()
    hass = FakeHass(config_entries)
    stop_calls = 0
    original_stop = AupuCoordinator.async_stop

    async def record_stop(coordinator: AupuCoordinator) -> None:
        nonlocal stop_calls
        stop_calls += 1
        await original_stop(coordinator)

    monkeypatch.setattr(AupuCoordinator, "async_stop", record_stop)
    monkeypatch.setattr(
        "custom_components.aupu_q360.async_get_clientsession",
        lambda _: object(),
    )

    with pytest.raises(ConfigEntryAuthFailed):
        _run(
            async_setup_entry(
                cast(HomeAssistant, hass),
                cast(ConfigEntry[AupuRuntimeData], entry),
            )
        )

    assert config_entries.forwarded is None
    assert stop_calls == 1
    assert entry.reauth_calls == 1
    assert "runtime_data" not in entry.__dict__
    assert issues.created[0][1:] == (
        "synthetic-entry_jwt_expired",
        {
            "is_fixable": False,
            "is_persistent": True,
            "severity": IssueSeverity.ERROR,
            "translation_key": "jwt_expired",
        },
    )


def test_setup_and_unload_own_one_enabled_wss_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
    issues: IssueRecorder,
) -> None:
    """Catch WSS starting twice, becoming a duplicate stopper, or surviving unload."""
    del issues
    entry = FakeEntry(
        data=_persisted_data(
            use_wss=True,
            user_uuid="synthetic-user-uuid",
        )
    )
    config_entries = FakeConfigEntries()
    hass = FakeHass(config_entries)
    starts: list[AupuShadowWebSocket] = []
    stops: list[AupuShadowWebSocket] = []

    async def record_start(client: AupuShadowWebSocket) -> None:
        starts.append(client)

    async def record_stop(client: AupuShadowWebSocket) -> None:
        stops.append(client)

    monkeypatch.setattr(AupuShadowWebSocket, "async_start", record_start)
    monkeypatch.setattr(AupuShadowWebSocket, "async_stop", record_stop)
    monkeypatch.setattr(
        "custom_components.aupu_q360.async_get_clientsession",
        lambda _: object(),
    )

    assert _run(
        async_setup_entry(
            cast(HomeAssistant, hass),
            cast(ConfigEntry[AupuRuntimeData], entry),
        )
    ) is True
    assert entry.runtime_data is not None
    assert len(starts) == 1
    assert entry.runtime_data.stoppers == [entry.runtime_data.coordinator]

    assert _run(
        async_unload_entry(
            cast(HomeAssistant, hass),
            cast(ConfigEntry[AupuRuntimeData], entry),
        )
    ) is True
    assert stops == starts
    assert "runtime_data" not in entry.__dict__


def test_missing_wss_user_uuid_requests_reauth_without_failing_setup(
    monkeypatch: pytest.MonkeyPatch,
    issues: IssueRecorder,
) -> None:
    """Catch incomplete historical WSS data crashing load or touching the network."""
    del issues
    entry = FakeEntry(data=_persisted_data(use_wss=True))
    config_entries = FakeConfigEntries()
    hass = FakeHass(config_entries)
    starts: list[AupuShadowWebSocket] = []
    original_start = AupuShadowWebSocket.async_start

    async def record_start(client: AupuShadowWebSocket) -> None:
        starts.append(client)
        await original_start(client)

    monkeypatch.setattr(AupuShadowWebSocket, "async_start", record_start)
    monkeypatch.setattr(
        "custom_components.aupu_q360.async_get_clientsession",
        lambda _: object(),
    )

    assert _run(
        async_setup_entry(
            cast(HomeAssistant, hass),
            cast(ConfigEntry[AupuRuntimeData], entry),
        )
    ) is True
    assert len(starts) == 1
    assert starts[0].is_running is False
    assert entry.reauth_calls == 1
    assert len(config_entries.entities) == 1
    assert config_entries.entities[0].is_on is None
    assert config_entries.entities[0].assumed_state is True

    assert _run(
        async_unload_entry(
            cast(HomeAssistant, hass),
            cast(ConfigEntry[AupuRuntimeData], entry),
        )
    ) is True
