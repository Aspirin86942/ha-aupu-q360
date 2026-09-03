# Q360 Discovery Transport Window Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Start every discovery on a fresh healthy WSS connection, allow five minutes per remote-operator phase, and finish before the observed hourly connection rollover.

**Architecture:** Add one serialized renew-and-wait-health lifecycle operation to the existing WSS transport, expose it through the coordinator, and inject it into discovery before any observer/archive/baseline work. Generate the new `300/3300` report limit profile while accepting complete legacy `120/3600` reports.

**Tech Stack:** Python 3.13.2+, asyncio, aiohttp, Home Assistant 2026.8 runtime, pytest, pytest-asyncio, Ruff, mypy, uv.

**Spec:** `docs/superpowers/specs/2026-09-03-q360-discovery-transport-window-design.md`

## Global Constraints

- Discovery sends only correlated Shadow `get`; never call device-control APIs or Shadow `update`/`desired`.
- WSS preparation timeout is 45 seconds; snapshot timeout is 10 seconds; stage timeout is 300 seconds; session timeout is 3,300 seconds.
- Active discovery still aborts on any WSS disconnect and never joins evidence across connections.
- Five discovery Action names, parameters, responses, experiment catalog, Store key, and report `schema_version: 2` remain unchanged.
- New reports emit only the `300/3300` limit profile; validation accepts exactly legacy `120/3600` or current `300/3300`, never a mixed pair.
- Raw archives remain opt-in, fixed-path, private, and content-free in logs/chat/Git.
- No new production dependency.
- Commit, push, HA sync/restart, and real-device execution remain separate rollout gates.

---

### Task 1: Renewable Healthy WSS Window

**Files:**
- Modify: `tests/test_wss.py`
- Modify: `custom_components/aupu_q360/wss.py`

**Interfaces:**
- Produces: `AupuShadowWebSocket.async_renew_and_wait_healthy(timeout_seconds: float = 45.0) -> None`
- Preserves: `async_start()`, `async_stop()`, `async_request_shadow_get()` and connection callback semantics.

- [ ] **Step 1: Write a failing renew success test**

Add a test using two `_ready_socket(auto_ping_response=True)` instances and `ControlledSleep`. Start the first socket, release its 30-second ping, then call `async_renew_and_wait_healthy()` in a task. Assert the first socket receives DISCONNECT and closes exactly once, the second socket fetches fresh credentials and subscribes exactly once, and the renew task returns only after the second PINGRESP.

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
uv run pytest -q tests/test_wss.py -k renew
```

Expected: failure because `async_renew_and_wait_healthy` does not exist.

- [ ] **Step 3: Implement serialized renew and health signaling**

Add a lifecycle lock and a generation-scoped health event. Route every connection callback through one helper that sets the event only for `(connected=True, healthy=True)` and clears it on disconnect. Implement renew as:

```python
async def async_renew_and_wait_healthy(self, timeout_seconds: float = 45.0) -> None:
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    async with self._lifecycle_lock:
        await self._async_stop_runner()
        self._healthy.clear()
        await self._async_start_runner()
        async with asyncio.timeout(timeout_seconds):
            await self._healthy.wait()
```

Keep public start/stop idempotent by acquiring the same lock and calling the private runner helpers. Preserve caller cancellation while awaiting stop or health.

- [ ] **Step 4: Add timeout/cancellation and stale-event tests**

Test a new socket that never returns PINGRESP: renew must time out, must not report healthy, and `async_stop()` must clean the remaining runner. Test that an old connection's health event cannot satisfy a new renewal.

- [ ] **Step 5: Verify Task 1 GREEN**

Run:

```bash
uv run pytest -q tests/test_wss.py
uv run ruff check custom_components/aupu_q360/wss.py tests/test_wss.py
```

Expected: all WSS tests pass with no task-leak warning.

### Task 2: Coordinator and Discovery Start Ordering

**Files:**
- Modify: `tests/test_light.py`
- Modify: `tests/test_discovery.py`
- Modify: `tests/test_config_flow.py`
- Modify: `custom_components/aupu_q360/coordinator.py`
- Modify: `custom_components/aupu_q360/discovery.py`
- Modify: `custom_components/aupu_q360/discovery_models.py`
- Modify: `custom_components/aupu_q360/__init__.py`

**Interfaces:**
- Consumes: `AupuShadowWebSocket.async_renew_and_wait_healthy(timeout_seconds=45.0)`.
- Produces: `AupuCoordinator.async_prepare_discovery_transport() -> None`.
- Changes constructor: `PanelStateDiscoverySession(..., prepare_transport: Callable[[], Awaitable[None]], ...)`.

- [ ] **Step 1: Write failing coordinator preparation tests**

Use the existing fake WSS lifecycle boundary. Assert preparation renews once, returns only with both coordinator flags true, preserves the last confirmed light value during the temporary disconnect, and rejects stopped, Reauth, missing-WSS, or missing-user-UUID states without a network call.

- [ ] **Step 2: Write failing discovery ordering tests**

Extend `DiscoveryHarness` with a controlled `prepare_transport`. Assert no sanitizer, archive, observer, session timer, or Shadow get exists while preparation is pending. After preparation succeeds, respond to the baseline and expect `discovery_ready_for_step`.

For preparation failure, assert `DiscoveryWssUnavailableError`, state idle after cleanup, no Shadow get, no saved report, and `last_manual_restore_required is False`.

- [ ] **Step 3: Run focused tests and verify RED**

Run:

```bash
uv run pytest -q tests/test_light.py -k prepare_discovery
uv run pytest -q tests/test_discovery.py -k 'prepare_transport or timeout'
```

Expected: failures because the new coordinator method and constructor argument do not exist.

- [ ] **Step 4: Implement coordinator and discovery flow**

Add the coordinator method with fixed error folding and post-renew connected/healthy verification. Add internal `DiscoveryState.TRANSPORT_PREPARING`. In `async_start`, await preparation before sanitizer/archive/observer creation; start the 3,300-second session timer only after preparation succeeds. Map all non-cancellation preparation failures to `DiscoveryWssUnavailableError` without exposing exception text.

Wire `coordinator.async_prepare_discovery_transport` in `async_setup_entry` and update all test constructors with a no-op or controlled async callable.

- [ ] **Step 5: Verify Task 2 GREEN**

Run:

```bash
uv run pytest -q tests/test_light.py tests/test_discovery.py tests/test_config_flow.py
uv run ruff check custom_components/aupu_q360/coordinator.py custom_components/aupu_q360/discovery.py custom_components/aupu_q360/discovery_models.py custom_components/aupu_q360/__init__.py tests/test_light.py tests/test_discovery.py tests/test_config_flow.py
```

Expected: all focused tests pass; no background task warning.

### Task 3: Limit Profile Compatibility and Runtime Boundary

**Files:**
- Modify: `tests/test_discovery_analysis.py`
- Modify: `tests/test_discovery_sanitizer.py`
- Modify: `tests/ha_runtime/test_ha_runtime.py`
- Modify: `custom_components/aupu_q360/discovery_analysis.py`
- Modify: `custom_components/aupu_q360/discovery_report_schema.py`

**Interfaces:**
- Produces new reports with `stage_timeout_seconds=300` and `session_timeout_seconds=3300`.
- Accepts validation pairs `(120, 3600)` and `(300, 3300)` only.

- [ ] **Step 1: Write failing limit-profile tests**

Assert a newly built report contains literal `300/3300`. Deep-copy it into a legacy `120/3600` report and assert validation succeeds. Create both mixed pairs (`120/3300`, `300/3600`) and assert `DiscoverySanitizationError`.

- [ ] **Step 2: Write failing HA runtime start-order test**

Use the real entry manager with fake WSS. Call `start_discovery`, verify the initial connection is closed, a second set of WSS credentials is fetched, the new connection reaches healthy, and only then a correlated discovery Shadow get is accepted. Assert no control service or Shadow update is called.

- [ ] **Step 3: Run focused tests and verify RED**

Run:

```bash
uv run pytest -q tests/test_discovery_analysis.py tests/test_discovery_sanitizer.py
uv run --group ha-test pytest -q -m ha_runtime tests/ha_runtime/test_ha_runtime.py -k discovery
```

Expected: limit assertions and renew ordering fail against `0.2.1` behavior.

- [ ] **Step 4: Implement exact compatible profiles**

Change report generation constants to `300/3300`. Validate all invariant limits first, then require the `(stage_timeout_seconds, session_timeout_seconds)` tuple to be in `{(120, 3600), (300, 3300)}`. Do not relax any other schema or sensitive scan.

- [ ] **Step 5: Verify Task 3 GREEN**

Run both RED commands again. Expected: all focused and HA runtime discovery tests pass.

### Task 4: Documentation and Version 0.2.2

**Files:**
- Modify: `README.md`
- Modify: `docs/q360-read-only-discovery-runbook.md`
- Modify: `docs/superpowers/specs/2026-09-03-q360-panel-state-discovery-v2-design.md`
- Modify: `docs/superpowers/specs/2026-09-03-q360-remote-operator-discovery-design.md`
- Modify: `custom_components/aupu_q360/manifest.json`
- Modify: `custom_components/aupu_q360/const.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `tests/test_manifest.py`

**Interfaces:**
- Publishes integration version `0.2.2` consistently.
- Documents a 45-second WSS preparation, 300-second stage, and 3,300-second session.

- [ ] **Step 1: Add failing manifest/document contract assertions**

Update version assertions to `0.2.2`. Require the operator docs to state that start renews WSS and waits for healthy before baseline, each phase has 300 seconds, and the full session ends at 3,300 seconds. Reject obsolete active guidance claiming `120/3600` for the current profile while retaining explicit legacy-report compatibility wording.

- [ ] **Step 2: Run and verify RED**

```bash
uv run pytest -q tests/test_manifest.py
```

Expected: current version and operator-limit assertions fail.

- [ ] **Step 3: Update docs and versions**

Synchronize the four version sources, run `uv lock`, and update human instructions. Do not alter the five Action schemas or translation key trees.

- [ ] **Step 4: Verify Task 4 GREEN**

```bash
uv run pytest -q tests/test_manifest.py
uv lock --check
```

Expected: PASS and lock file current.

### Task 5: Full Local Verification and Rollout Gate

**Files:**
- Verify only: entire repository

**Interfaces:**
- Produces an evidence-backed local release candidate; does not deploy it.

- [ ] **Step 1: Run the complete non-runtime suite**

```bash
uv run pytest -q
```

- [ ] **Step 2: Run HA runtime tests**

```bash
uv run --group ha-test pytest -q -m ha_runtime tests/ha_runtime
```

- [ ] **Step 3: Run static and privacy gates**

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy custom_components/aupu_q360
uv run python scripts/verify_private_signer.py
uv run python scripts/check_no_secrets.py
git diff --check
```

- [ ] **Step 4: Review scope and Git state**

Confirm only the design, plan, focused implementation/tests/docs, lock file, and version files changed. Confirm `.codegraph/`, credentials, raw archives, HA runtime files, and generated private material are absent from the diff.

- [ ] **Step 5: Stop at rollout gates**

Do not commit, push, sync the component, reload/restart HA, or start a real discovery until the corresponding authorization and current-state checks are satisfied.
