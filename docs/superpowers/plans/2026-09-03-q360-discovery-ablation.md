# Q360 Discovery Ablation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task in the main model. Steps use checkbox (`- [ ]`) syntax for tracking. Do not dispatch subagents.

**Goal:** Replace the persistent five-Action Q360 discovery system with a temporary three-Action, reported-only, in-memory probe, then remove every obsolete discovery/archive/report surface without guessing formal field mappings.

**Architecture:** `PanelStateProbe` takes one correlated baseline through the existing WSS and returns deterministic boolean/small-integer differences for each later correlated sample. Experiment meaning stays outside Home Assistant; the integration stores no probe evidence and clears all in-memory state on stop, failure, disconnect, unload, or HA stop.

**Tech Stack:** Python 3.13.2+, asyncio, aiohttp, Home Assistant 2026.8 runtime, pytest, pytest-asyncio, Ruff, mypy, uv.

**Spec:** `docs/superpowers/specs/2026-09-03-q360-discovery-ablation-design.md`

## Global Constraints

- The temporary public surface is exactly `start_probe`, `sample_probe`, and `stop_probe`; each accepts only `config_entry_id`.
- Probe transport uses the existing sole AWS IoT WSS, calls only correlated Shadow `get`, and never calls a device-control API or Shadow `update`/`desired`.
- Only matching target-device `get/accepted` `state.reported` completes a snapshot request.
- Public paths use exactly `service/<1-10 ASCII digits>/property/<1-10 ASCII digits>`.
- Retained values are exact booleans or exact integers in `-1000..1000`; strings, floats, nulls, objects, arrays, large integers, and timestamps are ignored.
- One snapshot retains at most 256 allowed paths; one response returns at most 128 changes; overflow fails and clears the probe.
- Snapshot timeout is 10 seconds; WSS renewal health timeout remains 45 seconds; there is no operator-stage or session timeout.
- Probe responses, snapshots, client tokens, and changes are never written to HA Store, Diagnostics, files, logs, or Config Entry data.
- Formal light parsing runs before the optional probe observer and remains independent of probe failures.
- Existing Config Entry data may contain the legacy `raw_archive_enabled` key; loading ignores it and no upgrade-only write is performed.
- Do not alter Config Entry credentials, read private material, create a raw directory, or modify Compose during local implementation.
- Do not bump the `0.2.4` version, push, tag, or publish while the temporary probe exists.
- Every path in code examples below is synthetic test data, not a claimed real Q360 field mapping.
- `.codegraph/` remains untouched and must not be staged.

---

### Task 1: Deterministic Scalar Snapshot and Diff

**Files:**

- Create: `custom_components/aupu_q360/probe.py`
- Create: `tests/test_probe.py`

**Interfaces:**

- Produces: `ProbeErrorCode`, `ProbeError`, `ProbeValue`, `ProbeChange`, `ProbeResponse`.
- Produces: `extract_probe_snapshot(state: object, device_id: str) -> dict[str, ProbeValue]`.
- Produces: `diff_probe_snapshots(before: Mapping[str, ProbeValue], after: Mapping[str, ProbeValue]) -> tuple[ProbeChange, ...]`.
- Consumes later: `PanelStateProbe` in Task 2 and `services.py` in Task 3.

- [ ] **Step 1: Write failing extraction and diff tests**

Create `tests/test_probe.py` with these exact pure-function cases. The service/property ids and values are
synthetic and assert only the probe contract:

```python
"""Tests for the temporary reported-only Q360 scalar probe."""

from __future__ import annotations

import pytest

from custom_components.aupu_q360.probe import (
    ProbeError,
    diff_probe_snapshots,
    extract_probe_snapshot,
)

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
            DEVICE_ID: {
                "6": {"properties": {str(index): index for index in range(1, 258)}}
            }
        }
    }
    with pytest.raises(ProbeError, match="probe_invalid_payload"):
        extract_probe_snapshot(oversized, DEVICE_ID)
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
uv run pytest -q tests/test_probe.py
```

Expected: collection fails because `custom_components.aupu_q360.probe` does not exist.

- [ ] **Step 3: Implement the exact scalar boundary**

Create `custom_components/aupu_q360/probe.py` with the following definitions. This step deliberately has no
async lifecycle yet:

```python
"""Temporary in-memory reported-only field probe for Q360 development."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from homeassistant.exceptions import HomeAssistantError

type ProbeErrorCode = Literal[
    "probe_busy",
    "probe_inactive",
    "probe_wss_unavailable",
    "probe_snapshot_timeout",
    "probe_invalid_payload",
]
type ProbeValue = bool | int
type PublicProbeValue = ProbeValue | None
type PublicProbeChange = dict[str, str | PublicProbeValue]
type ProbeResponse = dict[str, str | int | list[PublicProbeChange]]

_PATH_ID = re.compile(r"[0-9]{1,10}")
_MIN_INTEGER = -1000
_MAX_INTEGER = 1000
_MAX_PATHS = 256
_MAX_CHANGES = 128


class ProbeError(HomeAssistantError):
    """Expose one fixed probe error code without untrusted context."""

    def __init__(self, error_code: ProbeErrorCode) -> None:
        self.error_code = error_code
        super().__init__(error_code)

    def __repr__(self) -> str:
        return f"ProbeError(error_code={self.error_code!r})"


@dataclass(frozen=True, slots=True)
class ProbeChange:
    """One safe scalar change at a normalized synthetic path."""

    path: str
    before: PublicProbeValue
    after: PublicProbeValue

    def to_public(self) -> PublicProbeChange:
        return {"path": self.path, "before": self.before, "after": self.after}


def extract_probe_snapshot(state: object, device_id: str) -> dict[str, ProbeValue]:
    """Extract only bounded scalars from the target reported device root."""
    if not isinstance(state, dict) or not isinstance(device_id, str) or not device_id:
        raise ProbeError("probe_invalid_payload")
    reported = state.get("reported")
    if not isinstance(reported, dict):
        raise ProbeError("probe_invalid_payload")
    device_state = reported.get(device_id)
    if not isinstance(device_state, dict):
        raise ProbeError("probe_invalid_payload")

    snapshot: dict[str, ProbeValue] = {}
    for service_id, service in device_state.items():
        if not isinstance(service_id, str) or _PATH_ID.fullmatch(service_id) is None:
            raise ProbeError("probe_invalid_payload")
        if not isinstance(service, dict):
            raise ProbeError("probe_invalid_payload")
        if "properties" not in service:
            continue
        properties = service["properties"]
        if not isinstance(properties, dict):
            raise ProbeError("probe_invalid_payload")
        for property_id, value in properties.items():
            if not isinstance(property_id, str) or _PATH_ID.fullmatch(property_id) is None:
                raise ProbeError("probe_invalid_payload")
            retained = _retained_value(value)
            if retained is None:
                continue
            snapshot[f"service/{service_id}/property/{property_id}"] = retained
            if len(snapshot) > _MAX_PATHS:
                raise ProbeError("probe_invalid_payload")
    return snapshot


def diff_probe_snapshots(
    before: Mapping[str, ProbeValue],
    after: Mapping[str, ProbeValue],
) -> tuple[ProbeChange, ...]:
    """Return a bounded, deterministic union diff without bool/int coercion."""
    changes: list[ProbeChange] = []
    for path in sorted(before.keys() | after.keys()):
        before_present = path in before
        after_present = path in after
        before_value = before.get(path)
        after_value = after.get(path)
        if (
            before_present
            and after_present
            and type(before_value) is type(after_value)
            and before_value == after_value
        ):
            continue
        changes.append(
            ProbeChange(
                path=path,
                before=before_value if before_present else None,
                after=after_value if after_present else None,
            )
        )
        if len(changes) > _MAX_CHANGES:
            raise ProbeError("probe_invalid_payload")
    return tuple(changes)


def _retained_value(value: object) -> ProbeValue | None:
    if type(value) is bool:
        return value
    if type(value) is int and _MIN_INTEGER <= value <= _MAX_INTEGER:
        return value
    return None
```

- [ ] **Step 4: Run focused tests and static checks**

Run:

```bash
uv run pytest -q tests/test_probe.py
uv run ruff check custom_components/aupu_q360/probe.py tests/test_probe.py
uv run mypy custom_components/aupu_q360/probe.py
```

Expected: all commands pass; no raw state or identifier appears in exception text or repr.

- [ ] **Step 5: Commit the pure probe boundary**

```bash
git add custom_components/aupu_q360/probe.py tests/test_probe.py
git commit -m "feat(状态探针): 添加内存标量差异"
```

---

### Task 2: Correlated Probe Lifecycle and Failure Cleanup

**Files:**

- Modify: `custom_components/aupu_q360/probe.py`
- Modify: `tests/test_probe.py`

**Interfaces:**

- Consumes: `AcceptedShadow` from `shadow.py`.
- Consumes: `prepare_transport() -> Awaitable[None]`, `request_shadow_get(client_token: str) -> Awaitable[None]`, `activate_observer(observer, cancel) -> None`, `deactivate_observer() -> None`, and `probe_available() -> bool`.
- Produces: `PanelStateProbe.async_start() -> ProbeResponse`.
- Produces: `PanelStateProbe.async_sample() -> ProbeResponse`.
- Produces: `PanelStateProbe.async_stop_probe() -> ProbeResponse` and lifecycle `async_stop() -> None`.
- Produces: synchronous `observe_shadow(message: AcceptedShadow) -> None` and `cancel_from_transport() -> None`.

- [ ] **Step 1: Add a failing correlated start/sample/stop test**

Append this helper and test to `tests/test_probe.py`; `SimpleNamespace` avoids coupling this task to the later
removal of `RawShadowEvent`:

```python
import asyncio
from collections.abc import Callable
from types import SimpleNamespace
from typing import cast

from custom_components.aupu_q360.probe import PanelStateProbe
from custom_components.aupu_q360.shadow import AcceptedShadow


async def _wait_until(predicate: Callable[[], bool]) -> None:
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was not reached")


def _message(token: str, state: dict[str, object], *, kind: str = "get") -> AcceptedShadow:
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
    baseline = {
        "reported": {DEVICE_ID: {"6": {"properties": {"1": False, "2": 3}}}}
    }
    changed = {
        "reported": {DEVICE_ID: {"6": {"properties": {"1": True, "2": 4}}}}
    }

    start = asyncio.create_task(probe.async_start())
    await _wait_until(lambda: len(requests) == 1)
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
```

- [ ] **Step 2: Add failing cleanup tests**

Add cases that assert:

```python
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
```

Define the helpers in the same test file with these fixed in-memory callbacks. Do not use wall-clock sleep beyond
the injected `0.001` second timeout:

```python
async def _async_noop() -> None:
    return None


def _probe_without_responses(
    *, snapshot_timeout: float
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
```

- [ ] **Step 3: Run the lifecycle tests and verify RED**

Run:

```bash
uv run pytest -q tests/test_probe.py -k "correlated or inactive or timeout"
```

Expected: failures because `PanelStateProbe` and its lifecycle properties do not exist.

- [ ] **Step 4: Implement the lifecycle with one pending future**

Extend `probe.py` with these concrete types, fields, response helper, and method behavior:

```python
import asyncio
import secrets
from collections.abc import Awaitable, Callable

from .shadow import AcceptedShadow

type PrepareTransport = Callable[[], Awaitable[None]]
type RequestShadowGet = Callable[[str], Awaitable[None]]
type ProbeObserver = Callable[[AcceptedShadow], None]
type ActivateObserver = Callable[[ProbeObserver, Callable[[], None]], None]
type DeactivateObserver = Callable[[], None]


def _response(
    *,
    state: Literal["active", "inactive"],
    message_code: Literal["probe_started", "probe_sampled", "probe_stopped"],
    sample_count: int,
    changes: tuple[ProbeChange, ...] = (),
) -> ProbeResponse:
    return {
        "state": state,
        "message_code": message_code,
        "sample_count": sample_count,
        "changes": [change.to_public() for change in changes],
    }


class PanelStateProbe:
    """Own one ephemeral sequence of correlated reported snapshots."""

    def __init__(
        self,
        *,
        device_id: str,
        prepare_transport: PrepareTransport,
        request_shadow_get: RequestShadowGet,
        activate_observer: ActivateObserver,
        deactivate_observer: DeactivateObserver,
        probe_available: Callable[[], bool],
        snapshot_timeout: float = 10.0,
    ) -> None:
        if not device_id or snapshot_timeout <= 0:
            raise ValueError("invalid probe configuration")
        self._device_id = device_id
        self._prepare_transport = prepare_transport
        self._request_shadow_get = request_shadow_get
        self._activate_observer = activate_observer
        self._deactivate_observer = deactivate_observer
        self._probe_available = probe_available
        self._snapshot_timeout = snapshot_timeout
        self._operation_lock = asyncio.Lock()
        self._active = False
        self._observer_active = False
        self._snapshot: dict[str, ProbeValue] | None = None
        self._sample_count = 0
        self._pending_token: str | None = None
        self._pending_future: asyncio.Future[dict[str, ProbeValue]] | None = None

    @property
    def active(self) -> bool:
        return self._active

    @property
    def pending_token(self) -> str | None:
        return self._pending_token

    async def async_start(self) -> ProbeResponse:
        if self._operation_lock.locked() or self._active:
            raise ProbeError("probe_busy")
        async with self._operation_lock:
            try:
                await self._prepare_transport()
                if not self._probe_available():
                    raise ProbeError("probe_wss_unavailable")
                self._active = True
                self._activate_observer(self.observe_shadow, self.cancel_from_transport)
                self._observer_active = True
                self._snapshot = await self._async_snapshot()
                return _response(
                    state="active",
                    message_code="probe_started",
                    sample_count=0,
                )
            except asyncio.CancelledError:
                self._clear()
                raise
            except ProbeError:
                self._clear()
                raise
            except Exception:
                self._clear()
                raise ProbeError("probe_wss_unavailable") from None

    async def async_sample(self) -> ProbeResponse:
        if self._operation_lock.locked():
            raise ProbeError("probe_busy")
        async with self._operation_lock:
            if not self._active or self._snapshot is None:
                raise ProbeError("probe_inactive")
            try:
                current = await self._async_snapshot()
                changes = diff_probe_snapshots(self._snapshot, current)
                self._snapshot = current
                self._sample_count += 1
                return _response(
                    state="active",
                    message_code="probe_sampled",
                    sample_count=self._sample_count,
                    changes=changes,
                )
            except asyncio.CancelledError:
                self._clear()
                raise
            except ProbeError:
                self._clear()
                raise
            except Exception:
                self._clear()
                raise ProbeError("probe_wss_unavailable") from None

    async def async_stop_probe(self) -> ProbeResponse:
        async with self._operation_lock:
            sample_count = self._sample_count
            self._clear()
            return _response(
                state="inactive",
                message_code="probe_stopped",
                sample_count=sample_count,
            )

    async def async_stop(self) -> None:
        async with self._operation_lock:
            self._clear()

    def observe_shadow(self, message: AcceptedShadow) -> None:
        future = self._pending_future
        if (
            future is None
            or future.done()
            or message.topic_kind != "get"
            or message.client_token != self._pending_token
        ):
            return
        try:
            snapshot = extract_probe_snapshot(message.state, self._device_id)
        except ProbeError as err:
            future.set_exception(err)
        else:
            future.set_result(snapshot)

    def cancel_from_transport(self) -> None:
        self._clear(error=ProbeError("probe_wss_unavailable"))

    async def _async_snapshot(self) -> dict[str, ProbeValue]:
        if not self._active or not self._probe_available():
            raise ProbeError("probe_wss_unavailable")
        token = f"disc-{secrets.token_hex(16)}"
        future: asyncio.Future[dict[str, ProbeValue]] = (
            asyncio.get_running_loop().create_future()
        )
        self._pending_token = token
        self._pending_future = future
        try:
            await self._request_shadow_get(token)
            async with asyncio.timeout(self._snapshot_timeout):
                return await future
        except TimeoutError:
            raise ProbeError("probe_snapshot_timeout") from None
        finally:
            if self._pending_future is future:
                self._pending_token = None
                self._pending_future = None

    def _clear(self, *, error: ProbeError | None = None) -> None:
        future = self._pending_future
        self._pending_token = None
        self._pending_future = None
        if future is not None and not future.done():
            if error is None:
                future.cancel()
            else:
                future.set_exception(error)
        self._active = False
        self._snapshot = None
        self._sample_count = 0
        if self._observer_active:
            self._observer_active = False
            self._deactivate_observer()
```

Do not add a phase enum, session timer, last report, restoration flag, background task, arbitrary label, or logger.
If a test reveals that `_deactivate_observer()` can raise, preserve cleanup with `try/finally` and re-raise only a
fixed `ProbeError`; never include callback text.

- [ ] **Step 5: Run lifecycle and type verification**

Run:

```bash
uv run pytest -q tests/test_probe.py
uv run ruff check custom_components/aupu_q360/probe.py tests/test_probe.py
uv run ruff format --check custom_components/aupu_q360/probe.py tests/test_probe.py
uv run mypy custom_components/aupu_q360/probe.py
```

Expected: all pass; timeout, cancellation, invalid payload, and explicit stop leave `active == False` and
`pending_token is None`.

- [ ] **Step 6: Commit the lifecycle**

```bash
git add custom_components/aupu_q360/probe.py tests/test_probe.py
git commit -m "feat(状态探针): 添加关联采样生命周期"
```

---

### Task 3: Cut the HA Public Surface to Three Temporary Actions

**Files:**

- Modify: `custom_components/aupu_q360/__init__.py`
- Modify: `custom_components/aupu_q360/models.py`
- Modify: `custom_components/aupu_q360/services.py`
- Modify: `custom_components/aupu_q360/services.yaml`
- Modify: `custom_components/aupu_q360/config_flow.py`
- Modify: `custom_components/aupu_q360/diagnostics.py`
- Modify: `custom_components/aupu_q360/strings.json`
- Modify: `custom_components/aupu_q360/translations/zh-Hans.json`
- Modify: `README.md`
- Modify: `docs/q360-read-only-discovery-runbook.md`
- Modify: `tests/test_services.py`
- Modify: `tests/test_config_flow.py`
- Modify: `tests/test_diagnostics.py`
- Modify: `tests/test_light.py`
- Modify: `tests/test_manifest.py`
- Modify: `tests/ha_runtime/test_ha_runtime.py`

**Interfaces:**

- Consumes: `PanelStateProbe` from Task 2.
- Preserves temporarily: coordinator methods `async_prepare_discovery_transport()`, `async_request_shadow_get()`, `async_set_discovery_observer()`, and `discovery_available`; Task 5 renames and narrows them after old modules are deleted.
- Produces: `START_PROBE = "start_probe"`, `SAMPLE_PROBE = "sample_probe"`, and `STOP_PROBE = "stop_probe"`.
- Produces: `AupuRuntimeData.probe: PanelStateProbe` and no runtime discovery Store/session fields.

- [ ] **Step 1: Rewrite service tests to require only three Actions**

Replace discovery-specific fakes and assertions in `tests/test_services.py` with a fake exposing exactly
`async_start`, `async_sample`, `async_stop_probe`, and `async_stop`. Assert the registry behavior and handlers:

```python
class FakeProbe:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.error: ProbeError | None = None

    async def async_start(self) -> ProbeResponse:
        self.calls.append("start")
        if self.error is not None:
            raise self.error
        return {
            "state": "active",
            "message_code": "probe_started",
            "sample_count": 0,
            "changes": [],
        }

    async def async_sample(self) -> ProbeResponse:
        self.calls.append("sample")
        return {
            "state": "active",
            "message_code": "probe_sampled",
            "sample_count": 1,
            "changes": [
                {"path": "service/6/property/2", "before": 3, "after": 4}
            ],
        }

    async def async_stop_probe(self) -> ProbeResponse:
        self.calls.append("stop_probe")
        return {
            "state": "inactive",
            "message_code": "probe_stopped",
            "sample_count": 1,
            "changes": [],
        }

    async def async_stop(self) -> None:
        self.calls.append("stop")


@pytest.mark.asyncio
async def test_probe_actions_route_by_config_entry_and_return_exact_payloads() -> None:
    module = _module()
    probe = FakeProbe()
    hass = _hass(runtime_data=SimpleNamespace(probe=probe))
    module.async_register_probe_entry(hass, "entry-one")

    responses = []
    for action in (module.START_PROBE, module.SAMPLE_PROBE, module.STOP_PROBE):
        handler = hass.services.handlers[(DOMAIN, action)]
        responses.append(await handler(ServiceCall(DOMAIN, action, {"config_entry_id": "entry-one"})))

    assert [response["message_code"] for response in responses] == [
        "probe_started",
        "probe_sampled",
        "probe_stopped",
    ]
    assert probe.calls == ["start", "sample", "stop_probe"]
```

Also keep the existing two-entry register/unregister test shape, but change the expected set to the three probe
constants. Every voluptuous schema must reject extra keys, including `experiment`, `round`, and arbitrary labels.

- [ ] **Step 2: Make UI/config/diagnostics tests demand the ablated contract**

Change `tests/test_manifest.py`, `tests/test_config_flow.py`, and `tests/test_diagnostics.py` to assert:

```python
assert set(services) == {"start_probe", "sample_probe", "stop_probe"}
assert set(strings["services"]) == set(services)
assert set(strings["exceptions"]) == {
    "probe_busy",
    "probe_inactive",
    "probe_wss_unavailable",
    "probe_snapshot_timeout",
    "probe_invalid_payload",
}
for service in services.values():
    assert set(service["fields"]) == {"config_entry_id"}
assert "raw_archive_enabled" not in strings["options"]["step"]["init"]["data"]
assert "state_discovery" not in diagnostics
```

Require README and the runbook to contain `start_probe`, `sample_probe`, `stop_probe`, “临时开发工具”,
“不保存”, “约 23 次”, and the Shadow-invisibility escalation rule. Reject all five old Action names and all
raw archive/Store/report operator instructions from current user-facing sections.

- [ ] **Step 3: Run the public-contract tests and verify RED**

Run:

```bash
uv run pytest -q tests/test_services.py tests/test_config_flow.py tests/test_diagnostics.py tests/test_manifest.py
```

Expected: failures show the five old Actions, raw archive option, and persisted discovery diagnostics still exist.

- [ ] **Step 4: Replace `services.py` with the three-handler registry**

Keep the existing multi-entry domain registration pattern, but define this exact protocol and handler mapping:

```python
START_PROBE = "start_probe"
SAMPLE_PROBE = "sample_probe"
STOP_PROBE = "stop_probe"
_SERVICE_NAMES = (START_PROBE, SAMPLE_PROBE, STOP_PROBE)
_REGISTRY_KEY = f"{DOMAIN}.probe_services"


class _Probe(Protocol):
    async def async_start(self) -> ProbeResponse:
        """Start and baseline the temporary probe."""

    async def async_sample(self) -> ProbeResponse:
        """Return the next adjacent safe diff."""

    async def async_stop_probe(self) -> ProbeResponse:
        """Clear the probe and return a fixed response."""

    async def async_stop(self) -> None:
        """Clear lifecycle resources without a service response."""


def _register_domain_services(hass: HomeAssistant) -> None:
    async def handle_start(call: ServiceCall) -> ServiceResponse:
        return await _call_probe(_resolve_probe(hass, call).async_start)

    async def handle_sample(call: ServiceCall) -> ServiceResponse:
        return await _call_probe(_resolve_probe(hass, call).async_sample)

    async def handle_stop(call: ServiceCall) -> ServiceResponse:
        return await _call_probe(_resolve_probe(hass, call).async_stop_probe)

    handlers = {
        START_PROBE: handle_start,
        SAMPLE_PROBE: handle_sample,
        STOP_PROBE: handle_stop,
    }
    for service_name, handler in handlers.items():
        hass.services.async_register(
            DOMAIN,
            service_name,
            handler,
            _ENTRY_SCHEMA,
            supports_response=SupportsResponse.OPTIONAL,
        )


async def _call_probe(action: Callable[[], Awaitable[ProbeResponse]]) -> ServiceResponse:
    try:
        return await action()
    except ProbeError as err:
        raise ServiceValidationError(
            err.error_code,
            translation_domain=DOMAIN,
            translation_key=err.error_code,
        ) from None
```

`_resolve_probe()` must repeat the current exact entry-id/domain/loaded-runtime checks, read
`runtime_data.probe`, and require only the four protocol methods above. Rename register/unregister functions to
`async_register_probe_entry()` and `async_unregister_probe_entry()`.

- [ ] **Step 5: Wire `PanelStateProbe` and remove persistence from the loaded runtime**

In `models.py`, import `PanelStateProbe` only under `TYPE_CHECKING`, remove the three discovery fields, and make
the runtime tail exactly:

```python
    use_wss: bool = False
    user_uuid: str | None = field(default=None, repr=False)
    coordinator: AupuCoordinator = field(init=False)
    probe: PanelStateProbe = field(init=False, repr=False)
    stoppers: list[AsyncStopper] = field(default_factory=list)
```

Remove `raw_archive_enabled` from `AupuConfigEntryData`, `from_mapping()`, and `as_mapping()`. Because
`from_mapping()` already selects known keys rather than rejecting extras, a saved legacy key remains loadable but
is not re-emitted.

In `__init__.py`, delete report/archive/store construction. Reuse the existing observer-remover closure and build:

```python
    probe = PanelStateProbe(
        device_id=device.did,
        prepare_transport=coordinator.async_prepare_discovery_transport,
        request_shadow_get=coordinator.async_request_shadow_get,
        activate_observer=activate_observer,
        deactivate_observer=deactivate_observer,
        probe_available=lambda: coordinator.discovery_available,
    )
    entry.runtime_data.probe = probe
    entry.runtime_data.stoppers.extend((probe, coordinator))
```

Register the three Actions after coordinator start. On HA stop call `probe.cancel_from_transport()` so the sync
callback clears memory immediately. On unload unregister the entry before `_async_teardown_runtime()`. Remove
`async_remove_entry()` because no probe data belongs to HA Store.

- [ ] **Step 6: Remove the option and Diagnostics report surface**

Delete `_CONF_RAW_ARCHIVE_ENABLED` from `config_flow.py`, the Options selector, `_parse_user_input()`, and the
Options candidate mapping. Keep token, phone, WSS, and user UUID behavior byte-for-byte except for formatting.

In `diagnostics.py`, remove report imports and `_safe_discovery_report()`. The result must end after the existing
`assumed_state` field; do not expose `probe.active`, sample count, paths, values, token, or last error.

- [ ] **Step 7: Replace HA metadata and operator docs**

Make `services.yaml` contain exactly three entries sharing the `config_entry_id` selector. In both locale files,
replace old service/exception keys with three Action descriptions and five fixed probe errors. Required Chinese
meaning:

```json
{
  "start_probe": "建立临时 reported 基线，不控制设备。",
  "sample_probe": "返回与上一份关联快照之间的布尔值和小整数变化。",
  "stop_probe": "只清理探针内存和观察器，不恢复或控制设备。"
}
```

Rewrite the README discovery section and `docs/q360-read-only-discovery-runbook.md` around the adaptive sequence
from the spec. State explicitly that the WSS parser already sees decrypted Shadow JSON, so external HAR/PCAP is
not a default prerequisite; two empty or ambiguous single-variable repetitions mark the capability unconfirmed
and require a separate App-layer observation design.

- [ ] **Step 8: Update HA runtime wiring tests**

Replace the persisted report/archive HA runtime cases with one real service flow that:

1. sets up a `MockConfigEntry` with WSS and a fully in-memory socket;
2. calls `start_probe` and returns one correlated baseline;
3. calls `sample_probe` and returns the exact two synthetic changes;
4. calls `stop_probe` and asserts inactive response;
5. verifies no `state_discovery` diagnostics key, no `.storage` probe key, no control API call, and clean unload.

Retain all existing non-discovery tests for authentication, light, connectivity, teardown, and config reload.

- [ ] **Step 9: Run the complete suite before the cutover commit**

Run:

```bash
uv run pytest -q
AUPU_RUN_HA_RUNTIME=1 uv run --group ha-test pytest tests/ha_runtime -m ha_runtime -v
uv run ruff check .
uv run ruff format --check .
uv run mypy custom_components/aupu_q360
```

Expected: all pass while old discovery modules remain present but unreachable. The Action registry contains only
the three probe Actions, Config Entry Options contain no archive toggle, and diagnostics contain no probe data.

- [ ] **Step 10: Commit the public cutover**

```bash
git add README.md docs/q360-read-only-discovery-runbook.md custom_components/aupu_q360 tests
git commit -m "refactor(状态发现): 切换为三步内存探针"
```

---

### Task 4: Delete the Persistent Discovery System

**Files:**

- Delete: `custom_components/aupu_q360/discovery.py`
- Delete: `custom_components/aupu_q360/discovery_analysis.py`
- Delete: `custom_components/aupu_q360/discovery_catalog.py`
- Delete: `custom_components/aupu_q360/discovery_models.py`
- Delete: `custom_components/aupu_q360/discovery_report_schema.py`
- Delete: `custom_components/aupu_q360/discovery_sanitizer.py`
- Delete: `custom_components/aupu_q360/discovery_store.py`
- Delete: `custom_components/aupu_q360/raw_discovery_archive.py`
- Delete: `tests/test_discovery.py`
- Delete: `tests/test_discovery_analysis.py`
- Delete: `tests/test_discovery_catalog.py`
- Delete: `tests/test_discovery_network_boundary.py`
- Delete: `tests/test_discovery_sanitizer.py`
- Delete: `tests/test_discovery_store.py`
- Delete: `tests/test_raw_discovery_archive.py`
- Modify: `custom_components/aupu_q360/errors.py`
- Modify: `tests/test_manifest.py`

**Interfaces:**

- Consumes: the Task 3 production runtime has no import from any deleted module.
- Preserves: all `AupuError` subclasses used by authentication, API, WSS, light, and config flow.
- Produces: a package containing no `Discovery*` class and no persistent discovery file.

- [ ] **Step 1: Add a failing absence guard**

Add this test to `tests/test_manifest.py` before deleting files:

```python
def test_persistent_discovery_modules_are_absent(project_root: Path) -> None:
    component = project_root / "custom_components/aupu_q360"
    obsolete = {
        "discovery.py",
        "discovery_analysis.py",
        "discovery_catalog.py",
        "discovery_models.py",
        "discovery_report_schema.py",
        "discovery_sanitizer.py",
        "discovery_store.py",
        "raw_discovery_archive.py",
    }
    assert obsolete.isdisjoint(path.name for path in component.glob("*.py"))

    production_text = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(component.glob("*.py"))
    )
    for forbidden in (
        "RawDiscoveryArchive",
        "DiscoveryReportStore",
        "PanelStateDiscoverySession",
        "raw_archive_enabled",
        "state_discovery",
    ):
        assert forbidden not in production_text
```

- [ ] **Step 2: Verify the guard is RED**

Run:

```bash
uv run pytest -q tests/test_manifest.py -k persistent_discovery_modules_are_absent
```

Expected: failure listing the eight obsolete production files.

- [ ] **Step 3: Delete exact obsolete files and discovery-only errors**

Run the explicit non-recursive removal:

```bash
git rm \
  custom_components/aupu_q360/discovery.py \
  custom_components/aupu_q360/discovery_analysis.py \
  custom_components/aupu_q360/discovery_catalog.py \
  custom_components/aupu_q360/discovery_models.py \
  custom_components/aupu_q360/discovery_report_schema.py \
  custom_components/aupu_q360/discovery_sanitizer.py \
  custom_components/aupu_q360/discovery_store.py \
  custom_components/aupu_q360/raw_discovery_archive.py \
  tests/test_discovery.py \
  tests/test_discovery_analysis.py \
  tests/test_discovery_catalog.py \
  tests/test_discovery_network_boundary.py \
  tests/test_discovery_sanitizer.py \
  tests/test_discovery_store.py \
  tests/test_raw_discovery_archive.py
```

Delete `DiscoveryError` and every `Discovery*Error` subclass from `errors.py`. Keep `AupuError`,
`AupuAuthError`, `AupuRateLimitError`, `AupuTemporaryError`, and `AupuProtocolError` unchanged.

- [ ] **Step 4: Prove there are no dangling imports or contract strings**

Run:

```bash
rg -n "from \.discovery|RawDiscoveryArchive|DiscoveryReportStore|PanelStateDiscoverySession|raw_archive_enabled|state_discovery|start_discovery|begin_discovery_step|advance_discovery_step|finish_discovery|cancel_discovery" custom_components tests README.md docs/q360-read-only-discovery-runbook.md
```

Expected: no output. Historical files under `docs/superpowers/` are intentionally outside this command and remain
as design records.

- [ ] **Step 5: Run the complete suite after deletion**

Run:

```bash
uv run pytest -q
AUPU_RUN_HA_RUNTIME=1 uv run --group ha-test pytest tests/ha_runtime -m ha_runtime -v
uv run ruff check .
uv run ruff format --check .
uv run mypy custom_components/aupu_q360
```

Expected: all pass with the absence guard green.

- [ ] **Step 6: Commit the deletion**

```bash
git add custom_components/aupu_q360/errors.py tests/test_manifest.py
git commit -m "refactor(状态发现): 删除持久化发现系统"
```

---

### Task 5: Narrow the Shared Shadow Transport and Verify the Final Temporary Tree

**Files:**

- Modify: `custom_components/aupu_q360/shadow.py`
- Modify: `custom_components/aupu_q360/wss.py`
- Modify: `custom_components/aupu_q360/coordinator.py`
- Modify: `custom_components/aupu_q360/__init__.py`
- Modify: `tests/test_shadow.py`
- Modify: `tests/test_wss.py`
- Create: `tests/test_probe_network_boundary.py`
- Modify: `tests/test_probe.py`
- Modify: `tests/ha_runtime/test_ha_runtime.py`

**Interfaces:**

- Removes: `RawShadowEvent`, `AcceptedShadow.raw_event`, WSS `OutgoingRecorder`, and the optional recorder argument.
- Renames: `discovery_available` to `probe_available`.
- Renames: `async_prepare_discovery_transport()` to `async_prepare_probe_transport()`.
- Renames: `async_set_discovery_observer()` to `async_set_probe_observer()`.
- Preserves: `async_request_shadow_get(client_token: str) -> None` and the token format `disc-[0-9a-f]{32}`.

- [ ] **Step 1: Write failing transport-surface and network guards**

In `tests/test_shadow.py`, construct `AcceptedShadow` without `raw_event` and assert its repr contains neither
state nor token. In `tests/test_wss.py`, call `async_request_shadow_get(client_token)` with one argument and
remove outgoing-recorder tests.

Create `tests/test_probe_network_boundary.py` with a static guard and an end-to-end synthetic WSS test. The
static guard must parse `probe.py`, `services.py`, and `coordinator.py` with `ast`, rejecting names
`AupuApiClient`, `set_light`, `async_set_light`, `RawShadowEvent`, `DiscoveryReportStore`, and string literals
containing `/appapi/iot/control` or `/shadow/update`.

The end-to-end test must use the existing fake socket pattern and assert:

```python
assert fake_session.calls == 1
assert fake_api.control_calls == []
assert start_response["message_code"] == "probe_started"
assert sample_response == {
    "state": "active",
    "message_code": "probe_sampled",
    "sample_count": 1,
    "changes": [
        {"path": "service/6/property/2", "before": 3, "after": 4}
    ],
}
for packet in probe_publishes:
    assert packet.topic == f"$aws/things/{DEVICE.did}/shadow/get"
    assert set(json.loads(packet.payload)) == {"clientToken"}
assert coordinator.is_on is False
```

Send one synthetic `update/accepted` carrying a light change before each correlated get response and assert the
formal light state updates while the pending probe request remains incomplete.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
uv run pytest -q tests/test_shadow.py tests/test_wss.py tests/test_probe_network_boundary.py
```

Expected: failures show `raw_event`, outgoing recorder, discovery-named coordinator hooks, or the missing network
test integration.

- [ ] **Step 3: Remove raw-event retention from Shadow and WSS**

Make `AcceptedShadow` exactly:

```python
@dataclass(frozen=True, slots=True)
class AcceptedShadow:
    """One validated target Shadow message with private parsed state."""

    topic_kind: Literal["get", "update"]
    state: dict[str, Any] = field(repr=False)
    client_token: str | None = field(default=None, repr=False)
```

Delete `RawShadowEvent`. In `parse_accepted_shadow()`, return only `topic_kind`, `state`, and `client_token`.

In WSS rename `_DISCOVERY_TOKEN` to `_PROBE_TOKEN`, delete `OutgoingRecorder`, and implement the request body
without retaining a raw event:

```python
    async def async_request_shadow_get(self, client_token: str) -> None:
        """Send one correlated Shadow get only on the current ready connection."""
        if not isinstance(client_token, str) or _PROBE_TOKEN.fullmatch(client_token) is None:
            raise AupuProtocolError
        websocket = self._active_websocket
        if websocket is None:
            raise AupuProtocolError
        topic = f"$aws/things/{self._device.did}/shadow/get"
        payload = json.dumps(
            {"clientToken": client_token}, separators=(",", ":")
        ).encode("utf-8")
        async with self._send_lock:
            if websocket is not self._active_websocket:
                raise AupuProtocolError
            await websocket.send_bytes(encode_publish(topic, payload))
```

- [ ] **Step 4: Rename coordinator ownership to probe terminology**

Rename types and fields to `ProbeObserver`, `ProbeCancel`, `_probe_observer`, and `_probe_cancel`. Rename the
three public coordinator members listed in Interfaces and update `__init__.py` wiring. Keep this ordering in
`async_apply_shadow_message()`:

```python
        update = parse_light_shadow_update(self._device, message)
        if update is not None:
            self.async_apply_shadow_update(update)
        observer = self._probe_observer
        if observer is None:
            return
        try:
            observer(message)
        except Exception:
            _LOGGER.error("AUPU probe observer failed")
```

`async_stop()`, WSS disconnect, and WSS auth failure must clear/call the probe fields exactly where they handled
discovery. `async_request_shadow_get()` keeps its fixed public exception text but passes no recorder argument.

- [ ] **Step 5: Run focused and complete verification**

Run all commands from a clean shell process:

```bash
uv sync --locked
uv lock --check
uv run pytest -q
AUPU_RUN_HA_RUNTIME=1 uv run --group ha-test pytest tests/ha_runtime -m ha_runtime -v
uv run ruff check .
uv run ruff format --check .
uv run mypy custom_components/aupu_q360
uv run python scripts/verify_private_signer.py
uv run python scripts/check_no_secrets.py
git diff --check
```

Expected:

- offline and HA runtime tests pass;
- Ruff, formatting, mypy, lock check, and secret scan pass;
- the private signer check either passes with available private material or reports its designed safe skip with
  exit status 0;
- no command performs real network I/O or device control;
- `git diff --check` prints nothing.

- [ ] **Step 6: Audit the final temporary surface**

Run:

```bash
rg -n "discovery|raw_archive|RawShadowEvent|state_discovery|shadow/update|set_light" custom_components/aupu_q360/probe.py custom_components/aupu_q360/services.py docs/q360-read-only-discovery-runbook.md
git status --short
git diff --stat HEAD
```

Expected: the first command shows no forbidden implementation dependency; prose may use “discovery” only to
explain that the old system was removed. Status contains only Task 5 files plus the two uncommitted ablation
documents if they were intentionally kept outside implementation commits. `.codegraph/` has no status entry.

- [ ] **Step 7: Commit the transport cleanup**

```bash
git add custom_components/aupu_q360 tests
git commit -m "refactor(状态探针): 收窄只读 Shadow 边界"
```

Do not add the two ablation documents to this code commit unless the user separately requests their commit.

---

## Operational Handoff After Local Implementation

The five tasks end with a tested local temporary probe. They do not authorize runtime or external changes.

### Scope 1: Temporary Deployment

After one explicit scope authorization, re-check HEAD, worktree ownership, source/runtime paths, current HA
container, and live component hashes. Back up and sync only `custom_components/aupu_q360`, run HA
`check_config`, restart only the actual HA container, and verify:

- the Config Entry loads;
- connectivity and the formal light entity retain their prior semantics;
- exactly three probe Actions exist and the five old Actions do not;
- Options contain no raw archive toggle;
- Diagnostics contain no discovery/probe payload;
- no Store file, archive directory, or device command is created by smoke testing.

Do not push, tag, publish, modify Compose, or start a real probe in this scope.

### Scope 2: Adaptive Phone Experiment

After one explicit real-device scope authorization, re-check WSS connected/healthy, the empty/inactive probe,
paused automations, a safe bathroom, and an available household safety contact. The agent calls HA probe Actions;
the user only performs the requested single change in the official AUPU App or WeChat mini program.

Use the exact sequence from the spec: baseline, one idle sample, seven on/sample/off/sample pairs, then one
change/restore pair for fan level and temperature with their carrier modes. Repeat only ambiguous capabilities.
Stop immediately on WSS failure, unexpected device behavior, inability to restore, or two empty/ambiguous
single-variable repetitions. Empty/ambiguous results trigger a new App-layer observation design; they do not
permit guessed mappings.

The output of this scope is a factual list of exact observed paths, allowed value transitions, restoration
evidence, and unresolved capabilities. It is input to a new spec, not production code.

### Scope 3: Formal Mapping and Final Cleanup Deployment

Write a separate design and implementation plan only after Scope 2 provides exact evidence. That plan must name
every supported real path and omit unresolved capabilities. Its final tested tree deletes `probe.py`, all three
probe Actions, translations, probe docs, and probe tests before any version bump, push, tag, or release.

After one explicit cleanup/deployment scope authorization, re-check the exact live HA Store keys, Compose mount,
and private directory. Remove only the confirmed legacy discovery keys, the confirmed obsolete mount, and the
confirmed empty private directory; then sync the formal integration, validate HA config, restart HA, and verify
the live formal entities. Data deletion, Compose mutation, restart, and external push remain explicit operations
even though they belong to one named scope.

## Plan Completion Criteria

- Five independently committed TDD tasks leave only one temporary `probe.py` and three probe Actions.
- No old discovery module, report schema, Store, Diagnostics report, archive option, raw-event recorder, or old
  Action remains reachable.
- The full offline and HA runtime validation matrix passes from current command output.
- No real field is guessed and no external capture is treated as universally unnecessary.
- The temporary probe is deployed only locally and is removed by the later exact-mapping plan before release.
