"""State-machine tests for one bounded Q360 read-only discovery session."""

from __future__ import annotations

import asyncio
import importlib
import re
from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from custom_components.aupu_q360.discovery_sanitizer import (
    DiscoverySanitizer,
    validate_discovery_report,
)
from custom_components.aupu_q360.shadow import AcceptedShadow, RawShadowEvent

_DEVICE_ID = "123456789012345"


def _discovery():  # type: ignore[no-untyped-def]
    return importlib.import_module("custom_components.aupu_q360.discovery")


def _errors():  # type: ignore[no-untyped-def]
    return importlib.import_module("custom_components.aupu_q360.errors")


def _state(value: object, *, path: str = "1") -> dict[str, object]:
    return {
        "reported": {
            _DEVICE_ID: {"2": {"properties": {path: value}}},
        }
    }


class DiscoveryHarness:
    """Provide synthetic transport, Store, observer, and clock boundaries."""

    def __init__(
        self,
        *,
        snapshot_timeout: float = 1,
        step_timeout: float = 1,
        session_timeout: float = 1,
        max_changes: int = 256,
    ) -> None:
        self.tokens: list[str] = []
        self.requested = asyncio.Event()
        self.saved_reports: list[dict[str, object]] = []
        self.save_error = False
        self.available = True
        self.observer: Callable[[AcceptedShadow], None] | None = None
        self.transport_cancel: Callable[[], None] | None = None
        self.activations = 0
        self.deactivations = 0
        self.validations = 0
        module = _discovery()
        self.session = module.StateDiscoverySession(
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
            integration_version="0.1.0",
            now=lambda: datetime(2026, 9, 2, 13, 47, tzinfo=UTC),
            snapshot_timeout_seconds=snapshot_timeout,
            step_timeout_seconds=step_timeout,
            session_timeout_seconds=session_timeout,
            max_changes_per_step=max_changes,
        )

    async def request_shadow_get(self, token: str) -> None:
        self.tokens.append(token)
        self.requested.set()

    async def save_report(self, report: dict[str, object]) -> None:
        if self.save_error:
            raise RuntimeError("private-save-failure")
        self.saved_reports.append(report)

    def validate_report(self, report: object) -> None:
        self.validations += 1
        validate_discovery_report(
            report,
            forbidden_values=(_DEVICE_ID, "synthetic-entry-id"),
        )

    def activate_observer(
        self,
        observer: Callable[[AcceptedShadow], None],
        cancel: Callable[[], None],
    ) -> None:
        assert self.observer is None
        self.activations += 1
        self.observer = observer
        self.transport_cancel = cancel

    def deactivate_observer(self) -> None:
        assert self.observer is not None
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
        token: str,
        state: dict[str, object],
        *,
        topic_kind: str = "get",
    ) -> None:
        assert self.observer is not None
        self.observer(
            AcceptedShadow(
                topic_kind,
                state,
                token,
                raw_event=RawShadowEvent(
                    "incoming",
                    f"$aws/things/{_DEVICE_ID}/shadow/{topic_kind}/accepted",
                    b"{}",
                ),
            )
        )

    def update(self, state: dict[str, object]) -> None:
        assert self.observer is not None
        self.observer(
            AcceptedShadow(
                "update",
                state,
                raw_event=RawShadowEvent(
                    "incoming",
                    f"$aws/things/{_DEVICE_ID}/shadow/update/accepted",
                    b"{}",
                ),
            )
        )


async def _start_ready(harness: DiscoveryHarness, state: dict[str, object]) -> None:
    index = len(harness.tokens)
    task = asyncio.create_task(harness.session.async_start())
    token = await harness.token_at(index)
    harness.respond(token, state)
    await task


async def _begin_observing(
    harness: DiscoveryHarness,
    *,
    capability: str = "heating",
    target: str = "on",
    round_number: int = 1,
    state: dict[str, object] | None = None,
) -> None:
    index = len(harness.tokens)
    task = asyncio.create_task(harness.session.async_begin_step(capability, target, round_number))
    token = await harness.token_at(index)
    harness.respond(token, state or _state(False))
    await task


async def _complete_ready(
    harness: DiscoveryHarness,
    state: dict[str, object],
) -> None:
    index = len(harness.tokens)
    task = asyncio.create_task(harness.session.async_complete_step())
    token = await harness.token_at(index)
    harness.respond(token, state)
    await task


@pytest.mark.asyncio
async def test_normal_state_transitions_correlate_gets_and_finish_cleanly() -> None:
    """Catch snapshots being accepted out of order or raw session state surviving finish."""
    harness = DiscoveryHarness()
    module = _discovery()

    start_task = asyncio.create_task(harness.session.async_start())
    start_token = await harness.token_at(0)
    assert harness.session.state is module.DiscoveryState.BASELINING
    assert re.fullmatch(r"disc-[0-9a-f]{32}", start_token)

    harness.respond("disc-" + "f" * 32, _state(True))
    harness.respond(start_token, _state(True), topic_kind="update")
    await asyncio.sleep(0)
    assert not start_task.done()

    harness.respond(start_token, _state(False))
    await start_task
    assert harness.session.state is module.DiscoveryState.READY

    begin_task = asyncio.create_task(harness.session.async_begin_step("heating", "on", 1))
    begin_token = await harness.token_at(1)
    assert harness.session.state is module.DiscoveryState.STEP_BASELINING
    harness.respond(begin_token, _state(False))
    await begin_task
    assert harness.session.state is module.DiscoveryState.OBSERVING

    harness.update(_state(True))
    complete_task = asyncio.create_task(harness.session.async_complete_step())
    complete_token = await harness.token_at(2)
    assert harness.session.state is module.DiscoveryState.STEP_FINALIZING
    harness.respond(complete_token, _state(True))
    evidence = await complete_task
    assert evidence.label.evidence_id == "heating:on:1"
    assert evidence.changes[0].transient_count == 1
    assert harness.session.state is module.DiscoveryState.READY

    report = await harness.session.async_finish()

    assert report == harness.saved_reports[0]
    assert harness.validations == 1
    assert harness.session.state is module.DiscoveryState.IDLE
    assert harness.activations == harness.deactivations == 1
    assert harness.observer is None
    assert _DEVICE_ID not in repr(report)


@pytest.mark.asyncio
async def test_illegal_actions_fail_closed_without_damaging_the_active_session() -> None:
    """Catch actions skipping snapshots, overlapping sessions, or implicitly cancelling."""
    harness = DiscoveryHarness()
    errors = _errors()

    for action in (
        lambda: harness.session.async_begin_step("heating", "on", 1),
        harness.session.async_complete_step,
        harness.session.async_finish,
        harness.session.async_cancel,
    ):
        with pytest.raises(errors.DiscoveryInvalidTransitionError):
            await action()

    start_task = asyncio.create_task(harness.session.async_start())
    token = await harness.token_at(0)
    with pytest.raises(errors.DiscoveryBusyError):
        await harness.session.async_start()
    with pytest.raises(errors.DiscoveryInvalidTransitionError):
        await harness.session.async_finish()
    harness.respond(token, _state(False))
    await start_task

    with pytest.raises(errors.DiscoveryInvalidTransitionError):
        await harness.session.async_complete_step()

    begin_task = asyncio.create_task(harness.session.async_begin_step("heating", "on", 1))
    token = await harness.token_at(1)
    harness.respond(token, _state(False))
    await begin_task
    with pytest.raises(errors.DiscoveryBusyError):
        await harness.session.async_start()
    with pytest.raises(errors.DiscoveryInvalidTransitionError):
        await harness.session.async_begin_step("heating", "off", 1)
    with pytest.raises(errors.DiscoveryInvalidTransitionError):
        await harness.session.async_finish()

    await _complete_ready(harness, _state(True))
    assert harness.session.state.value == "ready"
    await harness.session.async_cancel()


@pytest.mark.asyncio
async def test_snapshot_timeout_uses_fixed_error_and_releases_every_reference() -> None:
    """Catch a missing get response retaining the HMAC key, waiter, or observer."""
    harness = DiscoveryHarness(snapshot_timeout=0.01)
    errors = _errors()

    with pytest.raises(errors.DiscoverySnapshotTimeoutError) as raised:
        await harness.session.async_start()

    assert str(raised.value) == "discovery_snapshot_timeout"
    assert harness.session.state.value == "idle"
    assert harness.activations == harness.deactivations == 1
    assert harness.session._session_key is None
    assert harness.session._pending_future is None
    assert harness.session._baseline is None
    assert harness.session._session_timer is None


@pytest.mark.asyncio
async def test_snapshot_requires_complete_target_reported_branch() -> None:
    """Catch an unrelated get response being mistaken for an empty device snapshot."""
    harness = DiscoveryHarness()
    errors = _errors()
    task = asyncio.create_task(harness.session.async_start())
    token = await harness.token_at(0)

    harness.respond(token, {"reported": {}})

    with pytest.raises(errors.DiscoverySnapshotTimeoutError):
        await task
    assert harness.session.state.value == "idle"
    assert harness.activations == harness.deactivations == 1


@pytest.mark.asyncio
async def test_step_and_session_deadlines_cancel_without_saving() -> None:
    """Catch expired experiment evidence remaining available for finish."""
    step_harness = DiscoveryHarness(step_timeout=0.01, session_timeout=1)
    await _start_ready(step_harness, _state(False))
    await _begin_observing(step_harness)
    await asyncio.sleep(0.03)

    assert step_harness.session.state.value == "idle"
    assert step_harness.session.last_error_code == "discovery_step_expired"
    assert step_harness.saved_reports == []
    assert step_harness.activations == step_harness.deactivations == 1

    session_harness = DiscoveryHarness(step_timeout=1, session_timeout=0.01)
    await _start_ready(session_harness, _state(False))
    await asyncio.sleep(0.03)

    assert session_harness.session.state.value == "idle"
    assert session_harness.session.last_error_code == "discovery_session_expired"
    assert session_harness.saved_reports == []
    assert session_harness.activations == session_harness.deactivations == 1


@pytest.mark.asyncio
async def test_resource_limit_invalidates_only_the_step_and_keeps_session_ready() -> None:
    """Catch a partial 257th change becoming candidate evidence or killing the WSS."""
    harness = DiscoveryHarness(max_changes=2)
    errors = _errors()
    await _start_ready(harness, _state(False))
    await _begin_observing(harness)

    harness.update(_state(True))
    harness.update(_state(False))
    harness.update(_state(True))

    with pytest.raises(errors.DiscoveryResourceLimitError):
        await harness.session.async_complete_step()

    assert harness.session.state.value == "ready"
    assert len(harness.tokens) == 2
    report = await harness.session.async_finish()
    assert report["statistics"]["invalid_steps"] == 1
    assert report["candidates"][0]["classification"] == "invalid"


@pytest.mark.asyncio
async def test_malformed_update_isolated_as_invalid_step_without_escaping_observer() -> None:
    """Catch hostile reported content reaching the coordinator or later evidence."""
    harness = DiscoveryHarness()
    errors = _errors()
    await _start_ready(harness, _state(False))
    await _begin_observing(harness)

    harness.update(_state(float("nan")))

    with pytest.raises(errors.DiscoveryResourceLimitError):
        await harness.session.async_complete_step()
    assert harness.session.state.value == "ready"
    await harness.session.async_cancel()


@pytest.mark.asyncio
async def test_transport_cancel_stop_and_external_cancellation_share_cleanup() -> None:
    """Catch disconnect, unload, or caller cancellation leaving an attached observer."""
    harness = DiscoveryHarness()
    await _start_ready(harness, _state(False))
    assert harness.transport_cancel is not None
    harness.transport_cancel()
    await asyncio.sleep(0)

    assert harness.session.state.value == "idle"
    assert harness.session.last_error_code == "discovery_wss_unavailable"
    assert harness.activations == harness.deactivations == 1

    pending = DiscoveryHarness()
    start_task = asyncio.create_task(pending.session.async_start())
    await pending.token_at(0)
    await pending.session.async_stop()
    with pytest.raises(_errors().DiscoveryWssUnavailableError):
        await start_task
    await pending.session.async_stop()
    assert pending.activations == pending.deactivations == 1

    cancelled = DiscoveryHarness()
    task = asyncio.create_task(cancelled.session.async_start())
    await cancelled.token_at(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.session.state.value == "idle"
    assert cancelled.activations == cancelled.deactivations == 1


@pytest.mark.asyncio
async def test_save_failure_is_fixed_and_old_report_boundary_remains_external() -> None:
    """Catch Store exception text leaking or a failed finish retaining session data."""
    harness = DiscoveryHarness()
    errors = _errors()
    await _start_ready(harness, _state(False))
    harness.save_error = True

    with pytest.raises(errors.DiscoveryReportSaveFailedError) as raised:
        await harness.session.async_finish()

    assert str(raised.value) == "discovery_report_save_failed"
    assert "private-save-failure" not in repr(raised.value)
    assert harness.saved_reports == []
    assert harness.session.state.value == "idle"
    assert harness.activations == harness.deactivations == 1
