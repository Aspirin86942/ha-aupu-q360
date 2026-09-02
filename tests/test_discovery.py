"""State-machine tests for one bounded Q360 v2 panel discovery session."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from custom_components.aupu_q360.discovery import PanelStateDiscoverySession
from custom_components.aupu_q360.discovery_catalog import build_step_request
from custom_components.aupu_q360.discovery_models import (
    DiscoveryPhase,
    DiscoveryState,
    JsonObject,
)
from custom_components.aupu_q360.discovery_report_schema import validate_discovery_report
from custom_components.aupu_q360.discovery_sanitizer import DiscoverySanitizer
from custom_components.aupu_q360.errors import (
    DiscoveryBusyError,
    DiscoveryInvalidParameterError,
    DiscoveryInvalidTransitionError,
    DiscoveryRawArchiveFailedError,
    DiscoveryRawArchiveUnavailableError,
    DiscoveryResourceLimitError,
    DiscoverySessionExpiredError,
    DiscoverySnapshotTimeoutError,
    DiscoveryStepExpiredError,
    DiscoveryWssUnavailableError,
)
from custom_components.aupu_q360.raw_discovery_archive import (
    ArchiveContext,
    RawArchiveMetadata,
)
from custom_components.aupu_q360.shadow import AcceptedShadow, RawShadowEvent

_DEVICE_ID = "123456789012345"
_GET_ACCEPTED = f"$aws/things/{_DEVICE_ID}/shadow/get/accepted"
_UPDATE_ACCEPTED = f"$aws/things/{_DEVICE_ID}/shadow/update/accepted"


def _state(value: object, *, path: str = "1") -> dict[str, object]:
    return {"reported": {_DEVICE_ID: {"2": {"properties": {path: value}}}}}


class FakeArchive:
    """Record exact archive ordering while leaving filesystem behavior to Task 3 tests."""

    def __init__(self, on_failure: Callable[[str], None], order: list[str]) -> None:
        self.on_failure = on_failure
        self.order = order
        self.events: list[tuple[RawShadowEvent, ArchiveContext]] = []
        self.fail_enqueue = False
        self.complete_error: Exception | None = None
        self.abort_calls = 0
        self.stop_calls = 0
        self.complete_calls = 0
        self._metadata = RawArchiveMetadata(
            enabled=True,
            status="open",
            session_id="rd-" + "a" * 32,
            event_count=0,
            file_bytes=0,
        )

    @property
    def metadata(self) -> RawArchiveMetadata:
        return self._metadata

    def enqueue(self, event: RawShadowEvent, context: ArchiveContext) -> None:
        self.order.append(f"archive:{event.direction}")
        if self.fail_enqueue:
            self.on_failure("discovery_raw_archive_failed")
            raise DiscoveryRawArchiveFailedError
        self.events.append((event, context))

    async def async_complete(self) -> RawArchiveMetadata:
        self.complete_calls += 1
        self.order.append("archive:complete")
        if self.complete_error is not None:
            raise self.complete_error
        self._metadata = RawArchiveMetadata(
            enabled=True,
            status="complete",
            session_id="rd-" + "a" * 32,
            event_count=len(self.events),
            file_bytes=123,
            sha256="b" * 64,
        )
        return self._metadata

    async def async_abort(self) -> RawArchiveMetadata:
        self.abort_calls += 1
        self.order.append("archive:abort")
        self._metadata = RawArchiveMetadata(
            enabled=True,
            status="incomplete",
            session_id="rd-" + "a" * 32,
            event_count=len(self.events),
            file_bytes=123,
        )
        return self._metadata

    async def async_stop(self) -> None:
        self.stop_calls += 1


class DiscoveryHarness:
    """Provide synthetic transport, Store, observer, archive, and timeout boundaries."""

    def __init__(
        self,
        *,
        archive_enabled: bool = False,
        archive_unavailable: bool = False,
        snapshot_timeout: float = 1,
        stage_timeout: float = 1,
        session_timeout: float = 1,
        max_changes: int = 256,
    ) -> None:
        self.tokens: list[str] = []
        self.recorders: list[Callable[[RawShadowEvent], None] | None] = []
        self.requested = asyncio.Event()
        self.saved_reports: list[JsonObject] = []
        self.save_error = False
        self.available = True
        self.request_error: Exception | None = None
        self.observer: Callable[[AcceptedShadow], None] | None = None
        self.transport_cancel: Callable[[], None] | None = None
        self.activations = 0
        self.deactivations = 0
        self.validations = 0
        self.order: list[str] = []
        self.archive: FakeArchive | None = None
        self.archive_unavailable = archive_unavailable

        async def archive_factory(on_failure: Callable[[str], None]) -> FakeArchive:
            self.order.append("archive:open")
            if self.archive_unavailable:
                raise DiscoveryRawArchiveUnavailableError
            self.archive = FakeArchive(on_failure, self.order)
            return self.archive

        self.session = PanelStateDiscoverySession(
            request_shadow_get=self.request_shadow_get,
            save_report=self.save_report,
            sanitizer_factory=lambda key: DiscoverySanitizer(
                session_key=key,
                device_id=_DEVICE_ID,
            ),
            validate_report=self.validate_report,
            activate_observer=self.activate_observer,
            deactivate_observer=self.deactivate_observer,
            discovery_available=lambda: self.available,
            integration_version="0.2.0",
            archive_factory=archive_factory if archive_enabled else None,
            now=lambda: datetime(2026, 9, 3, 0, 47, tzinfo=UTC),
            snapshot_timeout_seconds=snapshot_timeout,
            stage_timeout_seconds=stage_timeout,
            session_timeout_seconds=session_timeout,
            max_changes_per_phase=max_changes,
        )

    async def request_shadow_get(
        self,
        token: str,
        record_outgoing: Callable[[RawShadowEvent], None] | None,
    ) -> None:
        self.order.append("network:get")
        if self.request_error is not None:
            raise self.request_error
        self.tokens.append(token)
        self.recorders.append(record_outgoing)
        if record_outgoing is not None:
            record_outgoing(
                RawShadowEvent(
                    "outgoing",
                    f"$aws/things/{_DEVICE_ID}/shadow/get",
                    json.dumps({"clientToken": token}, separators=(",", ":")).encode(),
                )
            )
        self.requested.set()

    async def save_report(self, report: JsonObject) -> None:
        self.order.append("report:save")
        if self.save_error:
            raise RuntimeError("synthetic save detail")
        self.saved_reports.append(report)

    def validate_report(self, report: object) -> object:
        self.validations += 1
        return validate_discovery_report(
            report,
            forbidden_values=(_DEVICE_ID, "synthetic-entry-id"),
        )

    def activate_observer(
        self,
        observer: Callable[[AcceptedShadow], None],
        cancel: Callable[[], None],
    ) -> None:
        assert self.observer is None
        self.order.append("observer:activate")
        self.activations += 1
        self.observer = observer
        self.transport_cancel = cancel

    def deactivate_observer(self) -> None:
        if self.observer is not None:
            self.order.append("observer:deactivate")
            self.deactivations += 1
            self.observer = None
            self.transport_cancel = None

    async def token_at(self, index: int) -> str:
        async with asyncio.timeout(1):
            while len(self.tokens) <= index:
                await self.requested.wait()
                self.requested.clear()
        return self.tokens[index]

    def respond(
        self,
        token: str | None,
        state: dict[str, object],
        *,
        topic_kind: str = "get",
    ) -> None:
        assert self.observer is not None
        topic = _GET_ACCEPTED if topic_kind == "get" else _UPDATE_ACCEPTED
        payload = json.dumps({"clientToken": token, "state": state}, separators=(",", ":")).encode()
        self.observer(
            AcceptedShadow(
                topic_kind,  # type: ignore[arg-type]
                state,
                token,
                raw_event=RawShadowEvent("incoming", topic, payload),
            )
        )

    def update(self, state: dict[str, object]) -> None:
        self.respond(None, state, topic_kind="update")


async def _start_ready(harness: DiscoveryHarness, state: dict[str, object]) -> None:
    index = len(harness.tokens)
    task = asyncio.create_task(harness.session.async_start(all_modes_off_confirmed=True))
    token = await harness.token_at(index)
    harness.respond(token, state)
    progress = await task
    assert progress.state is DiscoveryState.READY
    assert progress.message_code == "discovery_ready_for_step"


async def _begin(
    harness: DiscoveryHarness,
    request,  # type: ignore[no-untyped-def]
    state: dict[str, object],
):  # type: ignore[no-untyped-def]
    index = len(harness.tokens)
    task = asyncio.create_task(harness.session.async_begin_step(request))
    token = await harness.token_at(index)
    harness.respond(token, state)
    return await task


async def _advance(
    harness: DiscoveryHarness,
    state: dict[str, object],
):  # type: ignore[no-untyped-def]
    index = len(harness.tokens)
    task = asyncio.create_task(harness.session.async_advance_step())
    token = await harness.token_at(index)
    harness.respond(token, state)
    return await task


@pytest.mark.asyncio
async def test_mode_cycle_advances_only_on_correlated_gets_and_finishes_cleanly() -> None:
    """Catch updates, wrong tokens, or workflow phases being mistaken for reported snapshots."""
    harness = DiscoveryHarness()

    with pytest.raises(DiscoveryInvalidParameterError):
        await harness.session.async_start(all_modes_off_confirmed=False)
    assert harness.tokens == []
    assert harness.activations == 0

    start_task = asyncio.create_task(harness.session.async_start(all_modes_off_confirmed=True))
    start_token = await harness.token_at(0)
    assert re.fullmatch(r"disc-[0-9a-f]{32}", start_token)
    assert harness.session.state is DiscoveryState.SESSION_BASELINING
    harness.respond("disc-" + "f" * 32, _state(False))
    harness.respond(start_token, _state(False), topic_kind="update")
    await asyncio.sleep(0)
    assert not start_task.done()
    harness.respond(start_token, _state(False))
    await start_task

    progress = await _begin(
        harness,
        build_step_request(experiment="night_light", round_number=1),
        _state(False),
    )
    assert progress.to_response() == {
        "state": "awaiting_operator",
        "message_code": "discovery_prompt_mode_on",
        "phase": "mode_on",
        "completed_cycle_count": 0,
        "manual_restore_required": False,
    }

    harness.update({"desired": {_DEVICE_ID: {"2": {"properties": {"1": True}}}}})
    progress = await _advance(harness, _state(True))
    assert progress.phase is DiscoveryPhase.MODE_RESTORE
    assert progress.message_code == "discovery_prompt_mode_restore"
    assert progress.manual_restore_required is True

    progress = await _advance(harness, _state(False))
    assert progress.state is DiscoveryState.READY
    assert progress.message_code == "discovery_cycle_recorded"
    assert progress.completed_cycle_count == 1
    assert progress.manual_restore_required is False

    report = await harness.session.async_finish()
    assert report == harness.saved_reports[0]
    assert report["schema_version"] == 2
    assert report["coverage"][6]["status"] == "partial"
    assert harness.validations == 1
    assert harness.session.state is DiscoveryState.IDLE
    assert harness.activations == harness.deactivations == 1
    assert harness.observer is None
    assert _DEVICE_ID not in repr(report)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("experiment", "request_kwargs", "expected_phases"),
    (
        (
            "global_fan_level",
            {"source_level": 3, "target_level": 5},
            (
                DiscoveryPhase.CARRIER_ON,
                DiscoveryPhase.PARAMETER_CHANGE,
                DiscoveryPhase.PARAMETER_RESTORE,
                DiscoveryPhase.CARRIER_OFF,
            ),
        ),
        (
            "ai_target_temperature",
            {"source_temperature": 35, "target_temperature": 36},
            (
                DiscoveryPhase.CARRIER_ON,
                DiscoveryPhase.PARAMETER_CHANGE,
                DiscoveryPhase.PARAMETER_RESTORE,
                DiscoveryPhase.CARRIER_OFF,
            ),
        ),
        (
            "idle_environment",
            {},
            (DiscoveryPhase.IDLE_OBSERVATION,),
        ),
    ),
)
async def test_parameter_and_idle_cycles_follow_catalog_phases(
    experiment: str,
    request_kwargs: dict[str, int],
    expected_phases: tuple[DiscoveryPhase, ...],
) -> None:
    """Catch callers skipping carrier/restoration phases or supplying their own phase."""
    harness = DiscoveryHarness()
    await _start_ready(harness, _state(0))
    request = build_step_request(
        experiment=experiment,
        round_number=1,
        **request_kwargs,
    )
    progress = await _begin(harness, request, _state(0))
    observed = [progress.phase]
    values = (
        (3, 5, 3, 0)
        if experiment == "global_fan_level"
        else (35, 36, 35, 0)
        if experiment == "ai_target_temperature"
        else (0,)
    )
    for value in values:
        progress = await _advance(harness, _state(value))
        if progress.phase is not None:
            observed.append(progress.phase)

    assert tuple(observed) == expected_phases
    assert progress.state is DiscoveryState.READY
    assert progress.completed_cycle_count == 1
    await harness.session.async_cancel()


@pytest.mark.asyncio
async def test_restore_required_retries_the_same_reference_and_attempt() -> None:
    """Catch failed restoration advancing a cycle or comparing retries to the failed snapshot."""
    harness = DiscoveryHarness()
    await _start_ready(harness, _state(False))
    await _begin(
        harness,
        build_step_request(experiment="night_light", round_number=1),
        _state(False),
    )
    await _advance(harness, _state(True))

    failed = await _advance(harness, _state(True))
    assert failed.state is DiscoveryState.RESTORE_REQUIRED
    assert failed.message_code == "discovery_restore_required"
    assert failed.phase is DiscoveryPhase.MODE_RESTORE
    assert failed.manual_restore_required is True
    with pytest.raises(DiscoveryInvalidTransitionError):
        await harness.session.async_begin_step(
            build_step_request(experiment="ventilation", round_number=1)
        )

    restored = await _advance(harness, _state(False))
    assert restored.state is DiscoveryState.READY
    cycle = harness.session.completed_cycles[0]
    restore_attempts = [
        phase.attempt for phase in cycle.phases if phase.phase is DiscoveryPhase.MODE_RESTORE
    ]
    assert restore_attempts == [1, 2]
    assert cycle.phases[-1].restorations[0].restored is True
    await harness.session.async_cancel()


@pytest.mark.asyncio
async def test_cross_cycle_consistency_rejects_duplicates_and_changed_sources() -> None:
    """Catch duplicate rounds, mixed fan sources, or reversed temperature pairs entering reports."""
    fan = DiscoveryHarness()
    await _start_ready(fan, _state(0))
    first = build_step_request(
        experiment="global_fan_level",
        round_number=1,
        source_level=3,
        target_level=5,
    )
    await _begin(fan, first, _state(0))
    for value in (3, 5, 3, 0):
        await _advance(fan, _state(value))
    for request in (
        first,
        build_step_request(
            experiment="global_fan_level",
            round_number=2,
            source_level=2,
            target_level=5,
        ),
    ):
        with pytest.raises(DiscoveryInvalidParameterError):
            await fan.session.async_begin_step(request)
    await fan.session.async_cancel()

    temperature = DiscoveryHarness()
    await _start_ready(temperature, _state(0))
    first_temperature = build_step_request(
        experiment="ai_target_temperature",
        round_number=1,
        source_temperature=35,
        target_temperature=36,
    )
    await _begin(temperature, first_temperature, _state(0))
    for value in (35, 36, 35, 0):
        await _advance(temperature, _state(value))
    with pytest.raises(DiscoveryInvalidParameterError):
        await temperature.session.async_begin_step(
            build_step_request(
                experiment="ai_target_temperature",
                round_number=2,
                source_temperature=36,
                target_temperature=35,
            )
        )
    await temperature.session.async_cancel()


@pytest.mark.asyncio
async def test_archive_opens_before_observer_and_network_and_completes_before_save() -> None:
    """Catch unarchived gets, late mount checks, or report save preceding archive durability."""
    harness = DiscoveryHarness(archive_enabled=True)
    await _start_ready(harness, _state(False))

    assert harness.order[:4] == [
        "archive:open",
        "observer:activate",
        "network:get",
        "archive:outgoing",
    ]
    assert harness.archive is not None
    assert [event.direction for event, _ in harness.archive.events[:2]] == [
        "outgoing",
        "incoming",
    ]
    report = await harness.session.async_finish()
    assert harness.order.index("archive:complete") < harness.order.index("report:save")
    assert report["raw_archive"]["status"] == "complete"
    assert harness.archive.complete_calls == 1
    assert harness.archive.abort_calls == 0


@pytest.mark.asyncio
async def test_unavailable_or_failed_archive_aborts_before_or_after_network_safely() -> None:
    """Catch raw archive failure silently degrading to a sanitized-only session."""
    unavailable = DiscoveryHarness(archive_enabled=True, archive_unavailable=True)
    with pytest.raises(DiscoveryRawArchiveUnavailableError):
        await unavailable.session.async_start(all_modes_off_confirmed=True)
    assert unavailable.tokens == []
    assert unavailable.activations == 0
    assert unavailable.session.state is DiscoveryState.IDLE

    failed = DiscoveryHarness(archive_enabled=True)
    start = asyncio.create_task(failed.session.async_start(all_modes_off_confirmed=True))
    token = await failed.token_at(0)
    assert failed.archive is not None
    failed.archive.fail_enqueue = True
    failed.respond(token, _state(False))
    with pytest.raises(DiscoveryRawArchiveFailedError):
        await start
    assert failed.session.state is DiscoveryState.IDLE
    assert failed.deactivations == 1
    assert failed.saved_reports == []


@pytest.mark.asyncio
async def test_cancel_and_transport_failure_report_only_evidence_based_restore_risk() -> None:
    """Catch cancellation claiming software restoration or omitting observed pending changes."""
    clean = DiscoveryHarness()
    await _start_ready(clean, _state(False))
    clean_progress = await clean.session.async_cancel()
    assert clean_progress.message_code == "discovery_cancelled"
    assert clean_progress.manual_restore_required is False

    changed = DiscoveryHarness()
    await _start_ready(changed, _state(False))
    await _begin(
        changed,
        build_step_request(experiment="night_light", round_number=1),
        _state(False),
    )
    await _advance(changed, _state(True))
    assert changed.transport_cancel is not None
    changed.transport_cancel()
    await changed.session.async_stop()

    assert changed.session.state is DiscoveryState.IDLE
    assert changed.session.last_failure_code == "discovery_wss_unavailable"
    assert changed.session.last_manual_restore_required is True
    assert changed.saved_reports == []


@pytest.mark.asyncio
async def test_snapshot_stage_and_session_timeouts_release_all_owned_work() -> None:
    """Catch any fixed deadline leaving observer, timer, token, or sanitizer state alive."""
    snapshot = DiscoveryHarness(snapshot_timeout=0.01, session_timeout=1)
    with pytest.raises(DiscoverySnapshotTimeoutError):
        await snapshot.session.async_start(all_modes_off_confirmed=True)
    assert snapshot.session.state is DiscoveryState.IDLE
    assert snapshot.observer is None

    stage = DiscoveryHarness(stage_timeout=0.01, session_timeout=1)
    await _start_ready(stage, _state(False))
    await _begin(
        stage,
        build_step_request(experiment="night_light", round_number=1),
        _state(False),
    )
    await asyncio.sleep(0.03)
    await stage.session.async_stop()
    assert stage.session.last_failure_code == DiscoveryStepExpiredError.error_code
    assert stage.session.state is DiscoveryState.IDLE

    session = DiscoveryHarness(stage_timeout=1, session_timeout=0.01)
    await _start_ready(session, _state(False))
    await asyncio.sleep(0.03)
    await session.session.async_stop()
    assert session.session.last_failure_code == DiscoverySessionExpiredError.error_code
    assert session.session.state is DiscoveryState.IDLE


@pytest.mark.asyncio
async def test_resource_limit_and_invalid_actions_fail_closed() -> None:
    """Catch oversized phase evidence or illegal actions corrupting an active session."""
    harness = DiscoveryHarness(max_changes=1)
    for action in (
        lambda: harness.session.async_begin_step(
            build_step_request(experiment="night_light", round_number=1)
        ),
        harness.session.async_advance_step,
        harness.session.async_finish,
        harness.session.async_cancel,
    ):
        with pytest.raises(DiscoveryInvalidTransitionError):
            await action()

    await _start_ready(harness, _state(False))
    with pytest.raises(DiscoveryBusyError):
        await harness.session.async_start(all_modes_off_confirmed=True)
    await _begin(
        harness,
        build_step_request(experiment="night_light", round_number=1),
        _state(False),
    )
    harness.update(_state(True, path="2"))
    harness.update(_state(True, path="3"))
    await asyncio.sleep(0)
    await harness.session.async_stop()
    assert harness.session.last_failure_code == DiscoveryResourceLimitError.error_code
    assert harness.session.state is DiscoveryState.IDLE


@pytest.mark.asyncio
async def test_request_failure_and_external_stop_cancellation_preserve_fixed_errors() -> None:
    """Catch transport details leaking or caller cancellation being swallowed by cleanup."""
    harness = DiscoveryHarness()
    harness.request_error = RuntimeError("synthetic transport detail")
    with pytest.raises(DiscoveryWssUnavailableError) as raised:
        await harness.session.async_start(all_modes_off_confirmed=True)
    assert str(raised.value) == "discovery_wss_unavailable"
    assert "synthetic" not in repr(raised.value)

    active = DiscoveryHarness()
    await _start_ready(active, _state(False))
    stop_task = asyncio.create_task(active.session.async_stop())
    stop_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stop_task
    await active.session.async_stop()
    assert active.session.state is DiscoveryState.IDLE


@pytest.mark.asyncio
async def test_stop_is_idempotent_and_clears_every_session_reference() -> None:
    """Catch unload or HA stop retaining raw snapshots, tokens, timers, writers, or observers."""
    harness = DiscoveryHarness(archive_enabled=True)
    await _start_ready(harness, _state(False))
    await harness.session.async_stop()
    await harness.session.async_stop()

    assert harness.session.state is DiscoveryState.IDLE
    assert harness.session.pending_token is None
    assert harness.session.completed_cycles == ()
    assert harness.observer is None
    assert harness.archive is not None
    assert harness.archive.abort_calls == 1
    assert harness.archive.stop_calls == 1
