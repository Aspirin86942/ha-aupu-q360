"""Tests for the temporary reported-only Q360 scalar probe."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from types import SimpleNamespace
from typing import cast

import pytest

from custom_components.aupu_q360.probe import (
    PanelStateProbe,
    ProbeError,
    diff_probe_snapshots,
    extract_probe_snapshot,
)
from custom_components.aupu_q360.shadow import AcceptedShadow

DEVICE_ID = "123456789"


def test_extract_probe_snapshot_keeps_only_normalized_safe_scalars() -> None:
    state = {
        "desired": {DEVICE_ID: {"6": {"properties": {"2": 5}}}},
        "reported": {
            DEVICE_ID: {
                "5": {"properties": {"1": False, "2": "private-text"}},
                "6": {"properties": {"2": 4, "3": 1001, "4": 1.5}},
                "7": {"properties": {"1": [1], "2": {"nested": True}}},
            }
        },
    }

    assert extract_probe_snapshot(state, DEVICE_ID) == {
        "service/5/property/1": False,
        "service/6/property/2": 4,
    }


def test_diff_probe_snapshots_is_sorted_and_preserves_type_and_absence() -> None:
    before = {
        "service/5/property/1": False,
        "service/6/property/2": 0,
        "service/8/property/1": True,
    }
    after = {
        "service/5/property/1": 0,
        "service/6/property/2": 4,
        "service/7/property/2": 36,
    }

    assert [change.to_public() for change in diff_probe_snapshots(before, after)] == [
        {"path": "service/5/property/1", "before": False, "after": 0},
        {"path": "service/6/property/2", "before": 0, "after": 4},
        {"path": "service/7/property/2", "before": None, "after": 36},
        {"path": "service/8/property/1", "before": True, "after": None},
    ]


def test_probe_snapshot_rejects_invalid_target_structure_and_fixed_limits() -> None:
    with pytest.raises(ProbeError, match="probe_invalid_payload"):
        extract_probe_snapshot({"reported": {DEVICE_ID: []}}, DEVICE_ID)

    oversized = {
        "reported": {
            DEVICE_ID: {"6": {"properties": {str(index): index for index in range(1, 258)}}}
        }
    }
    with pytest.raises(ProbeError, match="probe_invalid_payload"):
        extract_probe_snapshot(oversized, DEVICE_ID)


async def _wait_until(predicate: Callable[[], bool]) -> None:
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was not reached")


def _message(
    token: str,
    state: dict[str, object],
    *,
    kind: str = "get",
) -> AcceptedShadow:
    return cast(
        AcceptedShadow,
        SimpleNamespace(topic_kind=kind, client_token=token, state=state),
    )


@pytest.mark.asyncio
async def test_probe_uses_only_correlated_gets_and_clears_on_stop() -> None:
    requests: list[str] = []
    observer: Callable[[AcceptedShadow], None] | None = None
    cancel: Callable[[], None] | None = None

    async def prepare_transport() -> None:
        return None

    async def request_shadow_get(token: str) -> None:
        requests.append(token)

    def activate(
        candidate: Callable[[AcceptedShadow], None],
        cancellation: Callable[[], None],
    ) -> None:
        nonlocal observer, cancel
        observer = candidate
        cancel = cancellation

    def deactivate() -> None:
        nonlocal observer, cancel
        observer = None
        cancel = None

    probe = PanelStateProbe(
        device_id=DEVICE_ID,
        prepare_transport=prepare_transport,
        request_shadow_get=request_shadow_get,
        activate_observer=activate,
        deactivate_observer=deactivate,
        probe_available=lambda: True,
    )
    baseline = {"reported": {DEVICE_ID: {"6": {"properties": {"1": False, "2": 3}}}}}
    changed = {"reported": {DEVICE_ID: {"6": {"properties": {"1": True, "2": 4}}}}}

    start = asyncio.create_task(probe.async_start())
    await _wait_until(lambda: len(requests) == 1)
    assert re.fullmatch(r"disc-[0-9a-f]{32}", requests[0]) is not None
    assert observer is not None
    observer(_message("disc-00000000000000000000000000000000", changed))
    observer(_message(requests[0], changed, kind="update"))
    assert not start.done()
    observer(_message(requests[0], baseline))
    assert await start == {
        "state": "active",
        "message_code": "probe_started",
        "sample_count": 0,
        "changes": [],
    }

    sample = asyncio.create_task(probe.async_sample())
    await _wait_until(lambda: len(requests) == 2)
    assert observer is not None
    observer(_message(requests[1], changed))
    assert await sample == {
        "state": "active",
        "message_code": "probe_sampled",
        "sample_count": 1,
        "changes": [
            {"path": "service/6/property/1", "before": False, "after": True},
            {"path": "service/6/property/2", "before": 3, "after": 4},
        ],
    }

    assert await probe.async_stop_probe() == {
        "state": "inactive",
        "message_code": "probe_stopped",
        "sample_count": 1,
        "changes": [],
    }
    assert observer is None
    assert cancel is None
    assert probe.active is False
    assert probe.pending_token is None


@pytest.mark.asyncio
async def test_probe_rejects_inactive_sample_and_parallel_snapshot() -> None:
    requested = asyncio.Event()

    async def request_shadow_get(_: str) -> None:
        requested.set()

    probe = PanelStateProbe(
        device_id=DEVICE_ID,
        prepare_transport=_async_noop,
        request_shadow_get=request_shadow_get,
        activate_observer=lambda observer, cancel: None,
        deactivate_observer=lambda: None,
        probe_available=lambda: True,
    )
    with pytest.raises(ProbeError, match="probe_inactive"):
        await probe.async_sample()

    start = asyncio.create_task(probe.async_start())
    await requested.wait()
    with pytest.raises(ProbeError, match="probe_busy"):
        await probe.async_start()
    start.cancel()
    with pytest.raises(asyncio.CancelledError):
        await start
    assert probe.active is False
    assert probe.pending_token is None


@pytest.mark.asyncio
async def test_probe_timeout_and_transport_cancel_clear_every_reference() -> None:
    probe, get_cancel = _probe_without_responses(snapshot_timeout=0.001)
    with pytest.raises(ProbeError, match="probe_snapshot_timeout"):
        await probe.async_start()
    assert probe.active is False
    assert probe.pending_token is None

    probe, get_cancel = _probe_without_responses(snapshot_timeout=10.0)
    start = asyncio.create_task(probe.async_start())
    await _wait_until(lambda: probe.pending_token is not None)
    get_cancel()()
    with pytest.raises(ProbeError, match="probe_wss_unavailable"):
        await start
    assert probe.active is False
    assert probe.pending_token is None


async def _async_noop() -> None:
    return None


def _probe_without_responses(
    *,
    snapshot_timeout: float,
) -> tuple[PanelStateProbe, Callable[[], Callable[[], None]]]:
    installed_cancel: Callable[[], None] | None = None

    async def request_shadow_get(_: str) -> None:
        return None

    def activate(
        _: Callable[[AcceptedShadow], None],
        cancel: Callable[[], None],
    ) -> None:
        nonlocal installed_cancel
        installed_cancel = cancel

    def deactivate() -> None:
        nonlocal installed_cancel
        installed_cancel = None

    def get_cancel() -> Callable[[], None]:
        assert installed_cancel is not None
        return installed_cancel

    probe = PanelStateProbe(
        device_id=DEVICE_ID,
        prepare_transport=_async_noop,
        request_shadow_get=request_shadow_get,
        activate_observer=activate,
        deactivate_observer=deactivate,
        probe_available=lambda: True,
        snapshot_timeout=snapshot_timeout,
    )
    return probe, get_cancel
