# Q360 Panel State Discovery v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. The user selected main-model inline execution; do not dispatch subagents.

**Goal:** Replace the deployed v1 discovery workflow with a read-only v2 workflow that maps seven panel modes, a shared five-level selector, and the AI target temperature exclusively from correlated Shadow `reported` evidence while optionally retaining a permission-restricted raw local archive.

**Architecture:** Keep the existing single authenticated MQTT-over-WSS connection and decode each accepted target Shadow message once. Apply the confirmed lighting path first, then feed an optional discovery observer that records exact raw Shadow events outside Git and HA `/config`, sanitizes only the target `reported` subtree, and advances a multi-phase experiment session without inferring device state from workflow phases. Persist only a schema-v2 sanitized report under a new HA Store key; deployment and the real panel experiment remain separately authorized operations.

**Tech Stack:** Python 3.13, Home Assistant custom integration APIs, `aiohttp`, AWS IoT Device Shadow over the existing MQTT-over-WSS transport, `asyncio`, HA `Store`, `voluptuous`, `pytest`, `pytest-homeassistant-custom-component`, Ruff, mypy, and `uv`.

**Spec:** `docs/superpowers/specs/2026-09-03-q360-panel-state-discovery-v2-design.md`

## Global Constraints

- Work in `/home/george/projects/python/ha-aupu-q360`; do not copy private Config Entry material, raw captures, or runtime credentials into the checkout.
- Python remains `>=3.13.2`; use the checked-in `uv.lock` and project `uv` environment. Add no production dependency.
- Discovery may publish only `$aws/things/<did>/shadow/get`; it must never publish Shadow `update`/`desired` or call a mode, level, temperature, or lighting control API.
- The only confirmed device state source is the target device's Shadow `reported`; `desired`, a command result, a user statement, and a workflow phase are never confirmation.
- Preserve the existing light path `reported.<device-id>.2.properties.1`, reported-over-desired precedence, optimistic command state, stale-on-disconnect behavior, and one WSS connection.
- Fixed limits are: snapshot wait 10 seconds, operator stage 120 seconds, session 3,600 seconds, 256 sanitized changes per phase, 65,536 bytes per MQTT packet, and 64 MiB per encoded raw JSONL archive.
- Fixed host archive root is `/home/george/.local/state/ha-aupu-q360/raw-discovery/`; fixed container mount is `/var/lib/aupu-q360-private-discovery/`. The root/session directories are `0700`; files are `0600`.
- Raw archive is a boolean Config Entry option, defaults off, accepts no path, and fails closed when enabled but the fixed mount is unavailable.
- Raw topic/payload bytes may exist only in the private archive path and short-lived in-memory objects. They must not enter repr, logs, Action responses, HA Store, Diagnostics, tests, Git, HA `/config`, HA backups, or chat output.
- All automated fixtures use synthetic identifiers, tokens, topics, and Shadow documents. Do not read `config_entry-aupu_q360-01M1GKH9ZZ8JCB6XVDCY111TQ8.json` or any real Config Entry backup.
- `.codegraph/` is an existing untracked local index. Do not edit, stage, commit, delete, or deploy it.
- Local commits are part of implementation tasks. Push, host-directory creation, Compose changes, component synchronization, container recreation/restart, and the real Task 9 session each require their own current authorization.

## File Responsibility Map

- Create `custom_components/aupu_q360/discovery_catalog.py`: the sole source for experiment identifiers, kinds, carriers, phases, legal parameter ranges, prompt codes, and begin-step validation.
- Modify `custom_components/aupu_q360/discovery_models.py`: v2 secret-free enums and immutable request/evidence/progress models.
- Modify `custom_components/aupu_q360/shadow.py`: accepted Shadow parsing plus non-repr raw event retention.
- Modify `custom_components/aupu_q360/wss.py`: exact outgoing Shadow-get event recording before send; no second connection or replay queue.
- Modify `custom_components/aupu_q360/coordinator.py`: light-first dispatch and isolation of incoming/outgoing discovery observers.
- Create `custom_components/aupu_q360/raw_discovery_archive.py`: fixed-root validation, bounded queue, permission-safe JSONL writer, atomic completion, manifest, and metadata.
- Modify `custom_components/aupu_q360/discovery_sanitizer.py`: bounded numeric, string, array, and object representations with session HMAC comparison semantics.
- Modify `custom_components/aupu_q360/discovery_analysis.py`: phase diffing, restoration evaluation, coverage, dedicated/shared candidate analysis, and stable report construction.
- Create `custom_components/aupu_q360/discovery_report_schema.py`: exact schema-v2 validation and final sensitive-content scan.
- Rewrite `custom_components/aupu_q360/discovery.py`: the multi-phase `PanelStateDiscoverySession` and cleanup lifecycle.
- Modify `custom_components/aupu_q360/services.py`, `services.yaml`, `strings.json`, and `translations/zh-Hans.json`: five v2 Actions, selectors, fixed responses, prompts, and fixed errors.
- Modify `custom_components/aupu_q360/models.py`, `config_flow.py`, and `__init__.py`: boolean archive option and runtime wiring.
- Modify `custom_components/aupu_q360/discovery_store.py` and `diagnostics.py`: new v2 Store key, legacy removal policy, and sanitized Diagnostics only.
- Modify the focused `tests/test_*.py` files and `tests/ha_runtime/test_ha_runtime.py`; create `tests/test_raw_discovery_archive.py` and `tests/test_discovery_network_boundary.py`.
- Modify `README.md`, `docs/q360-read-only-discovery-runbook.md`, `custom_components/aupu_q360/manifest.json`, `custom_components/aupu_q360/const.py`, `pyproject.toml`, and mechanically refresh `uv.lock` for release `0.2.0`.

---

### Task 1: Establish the Fixed Experiment Catalog and v2 Types

**Files:**

- Create: `custom_components/aupu_q360/discovery_catalog.py`
- Modify: `custom_components/aupu_q360/discovery_models.py:1-177`
- Create: `tests/test_discovery_catalog.py`

**Interfaces:**

- Produces: `DiscoveryExperiment`, `ExperimentKind`, `DiscoveryPhase`, `DiscoveryState`, `DiscoveryCoverage`, `DiscoveryRound`, `DiscoveryStepRequest`, `DiscoveryProgress`, and `ExperimentDefinition`.
- Produces: immutable `EXPERIMENT_CATALOG`, `MODE_EXPERIMENTS`, `GLOBAL_FAN_LEVELS`, `AI_TARGET_TEMPERATURES`, and `PROMPT_CODE_BY_PHASE`.
- Produces: `build_step_request(*, experiment: str, round_number: object, source_level: object = None, target_level: object = None, source_temperature: object = None, target_temperature: object = None) -> DiscoveryStepRequest`.
- Produces: `definition_for(experiment: DiscoveryExperiment | str) -> ExperimentDefinition`.
- Consumes: no runtime, transport, HA, filesystem, or credential object.

- [ ] **Step 1: Write catalog and parameter-matrix tests**

Create tests that assert the exact catalog order and all fixed ranges:

```python
assert tuple(EXPERIMENT_CATALOG) == (
    DiscoveryExperiment.AI_THERMOSTATIC_WARMTH,
    DiscoveryExperiment.DEODORIZATION_STERILIZATION,
    DiscoveryExperiment.VENTILATION,
    DiscoveryExperiment.AIR_BLOWING,
    DiscoveryExperiment.NORMAL_DRYING,
    DiscoveryExperiment.THERMOSTATIC_DRYING,
    DiscoveryExperiment.NIGHT_LIGHT,
    DiscoveryExperiment.GLOBAL_FAN_LEVEL,
    DiscoveryExperiment.AI_TARGET_TEMPERATURE,
    DiscoveryExperiment.IDLE_ENVIRONMENT,
)
assert GLOBAL_FAN_LEVELS == (1, 2, 3, 4, 5)
assert AI_TARGET_TEMPERATURES == tuple(range(30, 43))
assert definition_for(DiscoveryExperiment.GLOBAL_FAN_LEVEL).carrier is (
    DiscoveryExperiment.VENTILATION
)
assert definition_for(DiscoveryExperiment.AI_TARGET_TEMPERATURE).carrier is (
    DiscoveryExperiment.AI_THERMOSTATIC_WARMTH
)
```

Parameterize accepted mode/idle requests and reject each invalid matrix case: round outside `1/2`; a mode or idle request with any parameter field; equal/out-of-range/non-integer/bool fan levels; missing one fan-level endpoint; temperatures outside `30..42`; bool/float temperatures; non-adjacent or equal temperatures; temperature fields on the fan experiment; level fields on the temperature experiment.

- [ ] **Step 2: Run the focused tests and confirm the red state**

Run:

```bash
uv run pytest tests/test_discovery_catalog.py -v
```

Expected: collection fails with `ModuleNotFoundError` for `discovery_catalog` or missing v2 symbols from `discovery_models`.

- [ ] **Step 3: Implement the catalog and request validator**

Define the public type surface exactly once:

```python
class DiscoveryExperiment(StrEnum):
    AI_THERMOSTATIC_WARMTH = "ai_thermostatic_warmth"
    DEODORIZATION_STERILIZATION = "deodorization_sterilization"
    VENTILATION = "ventilation"
    AIR_BLOWING = "air_blowing"
    NORMAL_DRYING = "normal_drying"
    THERMOSTATIC_DRYING = "thermostatic_drying"
    NIGHT_LIGHT = "night_light"
    GLOBAL_FAN_LEVEL = "global_fan_level"
    AI_TARGET_TEMPERATURE = "ai_target_temperature"
    IDLE_ENVIRONMENT = "idle_environment"


class DiscoveryPhase(StrEnum):
    SESSION_BASELINE = "session_baseline"
    STEP_BASELINE = "step_baseline"
    MODE_ON = "mode_on"
    MODE_RESTORE = "mode_restore"
    CARRIER_ON = "carrier_on"
    PARAMETER_CHANGE = "parameter_change"
    PARAMETER_RESTORE = "parameter_restore"
    CARRIER_OFF = "carrier_off"
    IDLE_OBSERVATION = "idle_observation"


class DiscoveryState(StrEnum):
    IDLE = "idle"
    ARCHIVE_OPENING = "archive_opening"
    SESSION_BASELINING = "session_baselining"
    READY = "ready"
    STEP_BASELINING = "step_baselining"
    AWAITING_OPERATOR = "awaiting_operator"
    RESTORE_REQUIRED = "restore_required"
    FINALIZING = "finalizing"
    CANCELLED = "cancelled"
```

Use `ExperimentDefinition(experiment, kind, carrier, phases)` and an immutable mapping. Mode phases are `(MODE_ON, MODE_RESTORE)`, parameter phases are `(CARRIER_ON, PARAMETER_CHANGE, PARAMETER_RESTORE, CARRIER_OFF)`, and idle phases are `(IDLE_OBSERVATION,)`.

`build_step_request()` accepts explicit optional fields and must test `type(value) is int`, not `isinstance(value, int)`, so booleans cannot enter numeric experiments. It returns an immutable request with a fixed `cycle_id`:

```python
mode cycle:        "night_light:1"
fan cycle:         "global_fan_level:3:5:2"
temperature cycle: "ai_target_temperature:35:36:1"
idle cycle:        "idle_environment:2"
```

`DiscoveryProgress.to_response()` may serialize only `state`, `message_code`, optional `phase`, `completed_cycle_count`, and `manual_restore_required`.

- [ ] **Step 4: Run catalog tests and static checks**

Run:

```bash
uv run pytest tests/test_discovery_catalog.py -v
uv run ruff check custom_components/aupu_q360/discovery_catalog.py custom_components/aupu_q360/discovery_models.py tests/test_discovery_catalog.py
uv run mypy custom_components/aupu_q360/discovery_catalog.py custom_components/aupu_q360/discovery_models.py
```

Expected: all commands pass; no v1 `heating`, `drying`, `swing`, `timer`, or three-level enum remains in the catalog/model API.

- [ ] **Step 5: Commit the catalog boundary**

```bash
git add custom_components/aupu_q360/discovery_catalog.py custom_components/aupu_q360/discovery_models.py tests/test_discovery_catalog.py
git diff --cached --check
git commit -m "feat(状态发现): 定义 Q360 v2 固定实验目录"
```

### Task 2: Preserve Exact Incoming and Outgoing Shadow Events Without Changing Lighting

**Files:**

- Modify: `custom_components/aupu_q360/shadow.py:1-132`
- Modify: `custom_components/aupu_q360/wss.py:39-122,282-301`
- Modify: `custom_components/aupu_q360/coordinator.py:28-31,74-91,174-181,246-280`
- Modify: `tests/test_shadow.py`
- Modify: `tests/test_wss.py`
- Modify: `tests/test_light.py`

**Interfaces:**

- Produces: `RawShadowEvent(direction: Literal["incoming", "outgoing"], topic: str, payload: bytes)` with `repr=False` fields and a fixed repr.
- Changes: `AcceptedShadow` gains `raw_event: RawShadowEvent = field(repr=False)` while retaining parsed `topic_kind`, `state`, and `client_token`.
- Changes: `AupuShadowWebSocket.async_request_shadow_get(client_token, record_outgoing=None) -> None` invokes `record_outgoing(event)` before the exact MQTT frame is sent.
- Changes: `AupuCoordinator.async_request_shadow_get(client_token, record_outgoing=None) -> None` remains the only discovery publish route.
- Preserves: `parse_light_shadow_update(device, accepted) -> LightShadowUpdate | None` and the existing strict light semantics.

- [ ] **Step 1: Extend parser tests with byte-exact raw retention and repr assertions**

Use synthetic bytes and require identity-preserving values:

```python
payload = b'{"clientToken":"disc-0123456789abcdef0123456789abcdef","state":{"reported":{"123":{"2":{"properties":{"1":true}}}}}}'
message = parse_accepted_shadow(DEVICE, GET_ACCEPTED, payload)
assert message is not None
assert message.raw_event.direction == "incoming"
assert message.raw_event.topic == GET_ACCEPTED
assert message.raw_event.payload == payload
assert GET_ACCEPTED not in repr(message)
assert payload.decode() not in repr(message)
assert "disc-0123456789abcdef0123456789abcdef" not in repr(message)
```

Keep every current invalid UTF-8/JSON/state/token test and every reported-over-desired light test.

- [ ] **Step 2: Add outgoing-recording order and failure tests**

After WSS readiness, capture the callback event and decoded MQTT packet:

```python
recorded: list[RawShadowEvent] = []
await client.async_request_shadow_get(TOKEN, recorded.append)
packet = decode_packets(websocket.sent[-1])[0]
assert recorded == [
    RawShadowEvent(
        direction="outgoing",
        topic="$aws/things/123456789/shadow/get",
        payload=b'{"clientToken":"disc-0123456789abcdef0123456789abcdef"}',
    )
]
assert packet.topic == recorded[0].topic
assert packet.payload == recorded[0].payload
```

Make the recorder raise a synthetic fixed exception and assert no frame is sent. Keep tests proving invalid tokens, unready/disconnected/stopped transports, and reconnects never queue or replay a discovery get.

- [ ] **Step 3: Run the transport red tests**

```bash
uv run pytest tests/test_shadow.py tests/test_wss.py tests/test_light.py -q
```

Expected: failures identify the missing raw-event field and the old one-argument requester signature; existing light tests still collect.

- [ ] **Step 4: Implement raw event plumbing and light-first coordinator dispatch**

Construct `RawShadowEvent` only for the two accepted target topics and for explicit correlated get requests. Do not retain WSS URL/query, MQTT CONNECT, credentials, PING, SUBSCRIBE, or control HTTP material.

The WSS send order is:

```python
event = RawShadowEvent("outgoing", topic, payload)
async with self._send_lock:
    if websocket is not self._active_websocket:
        raise AupuProtocolError
    if record_outgoing is not None:
        record_outgoing(event)  # a failure prevents an unarchived send
    await websocket.send_bytes(encode_publish(event.topic, event.payload))
```

The coordinator continues to parse and apply the light before discovery:

```python
update = parse_light_shadow_update(self._device, message)
if update is not None:
    self.async_apply_shadow_update(update)
observer = self._discovery_observer
if observer is not None:
    try:
        observer(message)
    except Exception:  # noqa: BLE001 - optional discovery is isolated
        _LOGGER.error("AUPU discovery observer failed")
```

Recorder/observer logs contain only fixed text. A recorder failure may stop discovery, but it cannot undo the already applied incoming light update.

- [ ] **Step 5: Verify transport and light compatibility**

```bash
uv run pytest tests/test_shadow.py tests/test_wss.py tests/test_light.py -q
uv run ruff check custom_components/aupu_q360/shadow.py custom_components/aupu_q360/wss.py custom_components/aupu_q360/coordinator.py tests/test_shadow.py tests/test_wss.py tests/test_light.py
uv run mypy custom_components/aupu_q360
```

Expected: pass; only explicit discovery gets have an outgoing recorder, the initial reconnect `{}` get remains unchanged, and accepted messages still update the light first.

- [ ] **Step 6: Commit the raw-event transport boundary**

```bash
git add custom_components/aupu_q360/shadow.py custom_components/aupu_q360/wss.py custom_components/aupu_q360/coordinator.py tests/test_shadow.py tests/test_wss.py tests/test_light.py
git diff --cached --check
git commit -m "feat(状态发现): 保留只读 Shadow 原始事件"
```

### Task 3: Implement the Permission-Safe Raw Discovery Archive

**Files:**

- Create: `custom_components/aupu_q360/raw_discovery_archive.py`
- Create: `tests/test_raw_discovery_archive.py`
- Modify: `custom_components/aupu_q360/errors.py:58-115`

**Interfaces:**

- Consumes: Task 1 controlled experiment/phase labels and Task 2 `RawShadowEvent`.
- Produces: `ArchiveContext(experiment, round, phase)` and `RawArchiveMetadata(enabled, status, session_id, event_count, file_bytes, sha256)`.
- Produces: `RawDiscoveryArchive.async_open(on_failure, *, root=RAW_ARCHIVE_ROOT, queue_limit=256, max_bytes=64 * 1024 * 1024, now=...)`.
- Produces: non-blocking `enqueue(event, context) -> None`, `async_complete() -> RawArchiveMetadata`, `async_abort() -> RawArchiveMetadata`, and idempotent `async_stop() -> None`.
- Produces fixed errors: `DiscoveryRawArchiveUnavailableError`, `DiscoveryRawArchiveFailedError`, and `DiscoveryRawArchiveLimitError`.

- [ ] **Step 1: Write fixed-root, permission, and symlink red tests**

Using `tmp_path`, create explicit roots and assert:

- missing root, a root symlink, non-directory root, mode other than `0700`, or non-writable root raises `discovery_raw_archive_unavailable` before a writer task starts;
- the session ID matches `rd-[0-9a-f]{32}` and cannot contain a device ID or time;
- the session directory is `0700`; `events.jsonl.partial`, `manifest.json`, and manifest temp files are `0600`;
- an existing session directory/file and a symlink at any created file path are rejected via exclusive/no-follow operations;
- the production default constant is exactly `Path("/var/lib/aupu-q360-private-discovery")` and callers cannot supply a path through any HA Action/config field.

- [ ] **Step 2: Write byte round-trip, ordering, completion, and failure tests**

Enqueue synthetic events with a controlled clock and then assert each JSONL line contains only:

```python
{
    "sequence": 1,
    "recorded_at_utc": "2026-09-03T00:00:01.000000Z",
    "experiment": "night_light",
    "round": 1,
    "phase": "mode_on",
    "direction": "incoming",
    "topic": "$aws/things/123456789/shadow/update/accepted",
    "payload_base64": "...",
}
```

Decode Base64 and compare exact bytes, including non-UTF-8 payloads. Require strictly increasing sequence numbers and queue order. On completion, verify flush/close, SHA-256 of the final `events.jsonl`, atomic `.partial` rename, exact byte/event counts, and a `complete` manifest.

Inject a queue limit of `1`, a small byte limit, write/fsync/rename/hash failures, cancellation, and stop. Each incomplete case keeps `events.jsonl.partial`, omits a success hash, writes `status=incomplete` when the filesystem permits, invokes the fixed failure callback once, and never logs raw content.

- [ ] **Step 3: Run archive tests and confirm the red state**

```bash
uv run pytest tests/test_raw_discovery_archive.py -v
```

Expected: collection fails because `raw_discovery_archive.py` and the three error classes do not exist.

- [ ] **Step 4: Implement exclusive creation and the bounded writer**

The production path is not created by integration code. `async_open()` first opens the root with `O_RDONLY | O_DIRECTORY | O_NOFOLLOW`, verifies the resulting descriptor with `fstat`, and keeps that descriptor as the authority for the session lifetime. Create the unpredictable session child with `dir_fd=root_fd`, open it again with `O_DIRECTORY | O_NOFOLLOW`, and perform every file create/rename/stat/hash operation relative to the session descriptor. This removes path-escape and symlink-swap races instead of relying only on a preflight `lstat`. Files use `O_CREAT | O_EXCL | O_NOFOLLOW` and exact modes. Every filesystem call runs behind `asyncio.to_thread`; `enqueue()` only validates controlled metadata, Base64-encodes into a bounded line, and uses `put_nowait`.

Use a single owner task and sentinel:

```python
def enqueue(self, event: RawShadowEvent, context: ArchiveContext) -> None:
    if self._failure_code is not None or self._closing:
        raise DiscoveryRawArchiveFailedError
    line = self._encode_line(event, context)
    if self._reserved_bytes + len(line) > self._max_bytes:
        self._fail(DiscoveryRawArchiveLimitError.error_code)
        raise DiscoveryRawArchiveLimitError
    try:
        self._queue.put_nowait(line)
    except asyncio.QueueFull:
        self._fail(DiscoveryRawArchiveFailedError.error_code)
        raise DiscoveryRawArchiveFailedError from None
    self._reserved_bytes += len(line)
```

Reserve sequence numbers and encoded bytes only after a successful enqueue, so pending queue bytes count toward the 64 MiB limit. The writer separately owns durable byte/event counters. `async_complete()` stops new events, drains the queue, verifies durable bytes equal reserved bytes, fsyncs, closes, hashes, atomically renames, fsyncs the session directory, then atomically writes the complete manifest. `async_abort()` drains already accepted events if possible, preserves `.partial`, and writes an incomplete manifest without a hash. Never automatically rotate, delete, upload, or interpret archives.

- [ ] **Step 5: Verify archive behavior and secret isolation**

```bash
uv run pytest tests/test_raw_discovery_archive.py -v
uv run ruff check custom_components/aupu_q360/raw_discovery_archive.py custom_components/aupu_q360/errors.py tests/test_raw_discovery_archive.py
uv run mypy custom_components/aupu_q360/raw_discovery_archive.py custom_components/aupu_q360/errors.py
```

Expected: pass; injected raw markers occur only inside the temporary archive files, never in repr, caplog, returned exceptions, or metadata.

- [ ] **Step 6: Commit the archive writer**

```bash
git add custom_components/aupu_q360/raw_discovery_archive.py custom_components/aupu_q360/errors.py tests/test_raw_discovery_archive.py
git diff --cached --check
git commit -m "feat(状态发现): 添加私有原始档案写入器"
```

### Task 4: Upgrade Session-Scoped Sanitization and Comparison Semantics

**Files:**

- Modify: `custom_components/aupu_q360/discovery_models.py`
- Modify: `custom_components/aupu_q360/discovery_sanitizer.py:1-507`
- Modify: `tests/test_discovery_sanitizer.py`

**Interfaces:**

- Preserves: `DiscoverySanitizer(session_key: bytes, device_id: str)` and `sanitize_reported(state) -> dict[str, SanitizedValue]`.
- Produces: `DiscoverySanitizer.close() -> None`; all later sanitization calls fail with the fixed `discovery_invalid_payload` error.
- Changes: `SanitizedValue.comparison` supports bounded scalar values or session-only HMAC fingerprints; `public` never contains raw strings, large numbers, container keys, or leaves.
- Produces: `canonicalize_and_fingerprint(value) -> tuple[fingerprint, depth, elements]` as a private bounded helper.

- [ ] **Step 1: Extend scalar boundary tests**

Require finite numbers in `[-1000, 1000]` to remain direct, timestamp-shaped numbers to expose only precision/delta semantics, and every other number to use a session HMAC:

```python
assert sanitize(-1000).public == {"type": "number", "value": -1000}
assert sanitize(1000).public == {"type": "number", "value": 1000}
large = sanitize(1001).public
assert large == {
    "type": "number",
    "representation": "fingerprint",
    "fingerprint": large["fingerprint"],
}
assert re.fullmatch(r"h-(?:[0-9a-f]{4}-){3}[0-9a-f]{4}", large["fingerprint"])
```

Use the existing Unix seconds/milliseconds ranges before the `[-1000, 1000]` rule. Assert same-session equality, cross-session unlinkability, bool rejection from number handling, and fixed rejection for NaN/infinity.

- [ ] **Step 2: Add canonical container-content tests**

Two dicts with different insertion order but identical JSON content must have the same HMAC. Arrays/objects with the same depth and element count but a changed key, leaf, order, boolean, or number must have a different HMAC. Public output is exactly `type`, `depth`, `elements`, and `fingerprint`; raw keys/leaves never occur in repr or serialized public values.

Keep limits at depth `4`, nodes `256`, nested key length `64`, packet-derived string length `65,536`, and reject non-string keys/non-JSON values. After `close()`, assert the key reference is cleared and no value can be sanitized.

- [ ] **Step 3: Run sanitizer red tests**

```bash
uv run pytest tests/test_discovery_sanitizer.py -v
```

Expected: failures show current large numbers are public and shape-identical containers compare equal.

- [ ] **Step 4: Implement stable bounded JSON HMAC**

Validate the container recursively first, then canonicalize with:

```python
canonical = json.dumps(
    validated,
    ensure_ascii=False,
    sort_keys=True,
    allow_nan=False,
    separators=(",", ":"),
).encode("utf-8")
fingerprint = self._fingerprint(canonical)
```

The comparison value for a string, large number, array, or object is `(kind, fingerprint)`; depth/elements remain descriptive only. Convert a large numeric value to a canonical JSON byte representation before HMAC so `1001` and `1001.0` follow explicit JSON type semantics. `close()` sets the stored key to `None`; `_fingerprint()` fails closed when closed.

- [ ] **Step 5: Verify sanitizer and existing diff tests**

```bash
uv run pytest tests/test_discovery_sanitizer.py tests/test_discovery_analysis.py -q
uv run ruff check custom_components/aupu_q360/discovery_models.py custom_components/aupu_q360/discovery_sanitizer.py tests/test_discovery_sanitizer.py
uv run mypy custom_components/aupu_q360/discovery_models.py custom_components/aupu_q360/discovery_sanitizer.py
```

Expected: pass; the old v1 analysis tests may require only type-constructor adaptations, not weakened assertions.

- [ ] **Step 6: Commit sanitizer v2**

```bash
git add custom_components/aupu_q360/discovery_models.py custom_components/aupu_q360/discovery_sanitizer.py tests/test_discovery_sanitizer.py tests/test_discovery_analysis.py
git diff --cached --check
git commit -m "feat(状态发现): 加强 v2 发现值脱敏"
```

### Task 5: Implement Phase Evidence, Restoration, Coverage, Candidates, and Schema v2

**Files:**

- Modify: `custom_components/aupu_q360/discovery_models.py`
- Rewrite: `custom_components/aupu_q360/discovery_analysis.py:1-299`
- Create: `custom_components/aupu_q360/discovery_report_schema.py`
- Rewrite: `tests/test_discovery_analysis.py`
- Modify: `tests/test_discovery_sanitizer.py`

**Interfaces:**

- Produces: `PhaseEvidence`, `PathRestoration`, `CycleEvidence`, and `RestorationResult` secret-free models.
- Consumes: Task 3 `RawArchiveMetadata`; the analysis/report layer must not know archive filesystem paths or handles.
- Preserves: `diff_snapshots(before, after, transient) -> tuple[SanitizedChange, ...]` with stable ordering.
- Produces: `background_paths(cycles) -> frozenset[str]`, `evaluate_restoration(...) -> RestorationResult`, and `confirmed_paths_for_experiment(...) -> frozenset[str]`.
- Produces: `build_discovery_report(*, integration_version: str, started_at: datetime, wss_baseline_succeeded: bool, archive: RawArchiveMetadata, cycles: Sequence[CycleEvidence]) -> JsonObject`.
- Produces: `validate_discovery_report(report, *, forbidden_values) -> ScanResult` in `discovery_report_schema.py`.

- [ ] **Step 1: Define red tests for phase evidence and path-level restoration**

Cover mode, parameter, carrier, and idle phases using synthetic `SanitizedValue` instances:

- mode restoration compares only paths changed in `mode_on` against the step baseline;
- parameter restoration compares only `parameter_change` paths against the carrier-local baseline;
- carrier-off compares only `carrier_on` paths against the step baseline;
- timestamps and paths established as repeatable idle-only background do not block restoration;
- a positive phase with no non-background changes is recorded as not observed and does not enter restore-required;
- when no experiment path is confirmed, at least one reversible non-background path permits completion while unrecovered paths remain ambiguous;
- once a path is confirmed for an experiment, failure to restore that path yields `required=True`.

Represent retry attempts explicitly:

```python
PhaseEvidence(
    phase=DiscoveryPhase.MODE_RESTORE,
    attempt=2,
    snapshot_succeeded=True,
    changes=changes,
    restorations=(PathRestoration(path=PATH, restored=True),),
)
```

- [ ] **Step 2: Define coverage and candidate red tests**

Test the following stable classification matrix:

- each of seven mode experiments needs two reversible, signature-identical cycles;
- `global_fan_level` is complete only when one source level is fixed and every other level has rounds 1 and 2; source-level return values participate in the value mapping without a no-change source-to-source cycle;
- `ai_target_temperature` requires the same adjacent source/target pair in rounds 1 and 2;
- no cycles -> `not_started`; only part of the required cycles -> `partial`; all required cycles -> `complete`;
- complete two-round evidence with no relevant changes -> `not_observed`; incomplete coverage never emits `not_observed`;
- invalid relevant evidence -> `invalid`; idle-only paths -> `observed_unidentified`;
- one path/one stable experiment -> `association=dedicated`;
- one path/multiple stable and pairwise-distinguishable enum or bitmask signatures -> each candidate is confirmed with `association=shared`;
- duplicate, non-repeatable, non-reversible, or background-only cross-experiment signatures -> `ambiguous`;
- string, large number, array, and object candidates use HMAC comparison while publishing only safe value mappings.

Candidate public values must include controlled label mappings, for example:

```python
{
    "experiment": "global_fan_level",
    "role": "parameter",
    "path": "service/5/property/2",
    "data_type": "number",
    "classification": "confirmed_candidate",
    "association": "dedicated",
    "value_mappings": [
        {"label": "level_3", "value": {"type": "number", "value": 3}},
        {"label": "level_5", "value": {"type": "number", "value": 5}},
    ],
    "evidence_cycles": [
        "global_fan_level:3:5:1",
        "global_fan_level:3:5:2",
    ],
}
```

Carrier candidates use `role=carrier` and the catalog carrier label; carrier changes never become parameter evidence.

- [ ] **Step 3: Define exact report and final-scan red tests**

The top-level report keys are exactly:

```python
{
    "schema_version": 2,
    "integration_version": "0.2.0",
    "session_started_utc_hour": "2026-09-03T00:00Z",
    "wss_baseline_succeeded": True,
    "raw_archive": archive_metadata,
    "coverage": coverage_rows,
    "cycles": cycle_rows,
    "candidates": candidate_rows,
    "limits": {
        "snapshot_timeout_seconds": 10,
        "stage_timeout_seconds": 120,
        "session_timeout_seconds": 3600,
        "max_changes_per_phase": 256,
        "mqtt_packet_bytes": 65_536,
        "raw_archive_bytes": 64 * 1024 * 1024,
    },
    "statistics": {
        "completed_cycles": completed,
        "invalid_cycles": invalid,
        "timeouts": timeouts,
        "restore_failures": restore_failures,
    },
    "sanitization_scan": {"passed": True, "finding_count": 0},
}
```

Disabled archive metadata is exactly `{"enabled": False, "status": "not_requested"}`. Complete archive metadata additionally contains only an `rd-<32 hex>` session ID, `complete`, event/byte counts, and a 64-hex SHA-256. No absolute path is legal.

Mutate every key/type/enum/count relationship and require the fixed `discovery_invalid_payload` error. Scan for forbidden device/entry/tag values, `$aws/things/`, `clientToken`, `payload`, `topic`, `desired`, Bearer/JWT/phone/signer markers, absolute archive paths, and raw synthetic strings without echoing findings.

- [ ] **Step 4: Run analysis/report tests and confirm the red state**

```bash
uv run pytest tests/test_discovery_analysis.py tests/test_discovery_sanitizer.py -q
```

Expected: failures show v1 capability grouping, whole-baseline restoration, cross-capability ambiguity, schema version 1, and the absent v2 schema module.

- [ ] **Step 5: Implement stable diffing, restoration, and background handling**

Keep actual before/after comparisons in memory only. `PhaseEvidence.to_public()` includes controlled experiment/round/phase/attempt, snapshot result, invalid/timeout flags, safe changes, and path/restored booleans. It never includes a token, device ID, raw value, or free text.

`background_paths()` always includes timestamp-valued paths and adds a non-timestamp path only when it changes in both completed idle rounds and appears in no non-idle positive phase. `evaluate_restoration()` receives the fixed reference snapshot, positive changes, candidate snapshot, known background paths, and previously confirmed paths. It returns restored/unrestored path sets and whether `RESTORE_REQUIRED` is mandatory under the specification's first-observation rule.

- [ ] **Step 6: Implement coverage and dedicated/shared candidate analysis**

Compute coverage rows in catalog order. For fan coverage, `required_rounds=8` and `complete` requires both rounds for all four targets other than the session source. For temperature, `required_rounds=2` and both cycles must share the same adjacent pair. For modes/idle, `required_rounds=2`.

Analyze only the phase assigned to each role:

```text
mode       -> mode_on + mode_restore
parameter  -> parameter_change + parameter_restore
carrier    -> carrier_on + carrier_off
idle       -> idle_observation only
```

First establish per-experiment repeatability and reversibility. Then group provisionally confirmed rows by path: one row is dedicated; multiple rows remain confirmed/shared only when their value/signature mappings are pairwise distinguishable. Stable sorting is catalog order, role order, path, and evidence-cycle ID.

- [ ] **Step 7: Implement the schema-v2 validator and scanner**

Move report validation out of the sanitizer. Validate exact keys, count recomputation, coverage consistency, evidence ID regexes, safe paths, HMAC formats, archive metadata variants, cycle/phase bounds, and the fixed limits. Only after structural validation serialize with `allow_nan=False`, scan forbidden markers/values, and return `ScanResult(passed=True, finding_count=0)`.

- [ ] **Step 8: Verify analysis and schema v2**

```bash
uv run pytest tests/test_discovery_analysis.py tests/test_discovery_sanitizer.py -q
uv run ruff check custom_components/aupu_q360/discovery_models.py custom_components/aupu_q360/discovery_analysis.py custom_components/aupu_q360/discovery_report_schema.py tests/test_discovery_analysis.py tests/test_discovery_sanitizer.py
uv run mypy custom_components/aupu_q360/discovery_models.py custom_components/aupu_q360/discovery_analysis.py custom_components/aupu_q360/discovery_report_schema.py
```

Expected: pass with deterministic report equality across input ordering; incomplete experiments are visible only through coverage and never mislabeled `not_observed`.

- [ ] **Step 9: Commit v2 evidence and reporting**

```bash
git add custom_components/aupu_q360/discovery_models.py custom_components/aupu_q360/discovery_analysis.py custom_components/aupu_q360/discovery_report_schema.py tests/test_discovery_analysis.py tests/test_discovery_sanitizer.py
git diff --cached --check
git commit -m "feat(状态发现): 生成 Q360 v2 阶段证据报告"
```

### Task 6: Replace the v1 Session With the Multi-Phase Panel State Machine

**Files:**

- Rewrite: `custom_components/aupu_q360/discovery.py:1-467`
- Rewrite: `tests/test_discovery.py`
- Modify: `custom_components/aupu_q360/errors.py`

**Interfaces:**

- Produces: `PanelStateDiscoverySession`.
- Consumes: `request_shadow_get(client_token: str, record_outgoing: Callable[[RawShadowEvent], None] | None) -> Awaitable[None]`.
- Consumes: `save_report(report: JsonObject) -> Awaitable[None]`, `validate_report(report: object) -> object`, `sanitizer_factory(session_key: bytes) -> DiscoverySanitizer`, and optional `archive_factory(on_failure: Callable[[str], None]) -> Awaitable[RawDiscoveryArchive]`.
- Consumes: `activate_observer(observer: Callable[[AcceptedShadow], None], cancel: Callable[[], None]) -> None`, `deactivate_observer() -> None`, `discovery_available() -> bool`, and integration version.
- Produces Actions methods: `async_start(all_modes_off_confirmed: bool) -> DiscoveryProgress`, `async_begin_step(request: DiscoveryStepRequest) -> DiscoveryProgress`, `async_advance_step() -> DiscoveryProgress`, `async_finish() -> JsonObject`, `async_cancel() -> DiscoveryProgress`, and `async_stop() -> None`.
- Produces callbacks: `async_observe_shadow(message: AcceptedShadow) -> None` and `cancel_from_transport(error_code: str) -> None`.

- [ ] **Step 1: Write complete legal-transition red tests**

Drive correlated synthetic get/accepted responses and assert:

```text
IDLE -> ARCHIVE_OPENING -> SESSION_BASELINING -> READY
READY -> STEP_BASELINING -> AWAITING_OPERATOR
AWAITING_OPERATOR -> next AWAITING_OPERATOR or READY
failed restoration -> RESTORE_REQUIRED -> retry same phase -> READY/next phase
READY -> FINALIZING -> IDLE
any active state -> CANCELLED -> IDLE
```

Test all cycles:

- mode begin snapshot, `mode_on`, `mode_restore`;
- parameter begin snapshot, `carrier_on`, `parameter_change`, `parameter_restore`, `carrier_off`;
- idle begin snapshot, `idle_observation`;
- no stage transition occurs from an update/accepted message alone; only a correlated full get/accepted snapshot completes an advance;
- `desired` may be archived but never changes snapshots, restoration, coverage, or candidate evidence.

At every state, parameterize illegal start/begin/advance/finish/cancel calls and assert the fixed error without corrupting a valid active session.

Call `async_start(False)` directly and require a fixed invalid-parameter error before archive, observer, or network work. Finish from `READY` after only one valid cycle and assert a schema-v2 report is saved with `partial`/`not_started` coverage, proving incomplete coverage is allowed only when no panel restoration is pending.

- [ ] **Step 2: Write cross-cycle consistency and restore-required tests**

Require each mode/idle round once, one session-wide fan source level, every fan source/target/round once, and one identical temperature pair for both rounds. Duplicates, changed fan source, reversed or different temperature pair, and beginning another cycle during restore-required fail with `discovery_invalid_parameter` or `discovery_invalid_transition`.

For a failed restore, assert repeated `advance` snapshots compare against the original reference, append attempt `2`, preserve the current experiment, reset only the 120-second stage timer, and return fixed `discovery_restore_required`. A later valid snapshot continues. Cancel/timeout/disconnect while panel changes lack restoration evidence returns `manual_restore_required=True` and fixed `discovery_manual_restore_required`; no software control call occurs.

- [ ] **Step 3: Write archive lifecycle, timeout, and cleanup tests**

Assert startup order: availability check, archive open if enabled, observer activation, outgoing baseline archive event, correlated incoming baseline. An unavailable archive fails before observer activation or network request.

Inject archive write/queue/limit failures and require session cancellation, `.partial` preservation, old report preservation, and no light rollback. Test 10-second snapshot, 120-second per-stage, and 3,600-second session limits with controlled sleepers. Stage advance resets only the stage timer; the session timer never resets.

After finish/cancel/failure/disconnect/auth failure/unload/HA stop, assert no pending future, token, snapshot, cycle, transient, sanitizer key, timer, writer task, or observer reference remains. `async_stop()` awaits scheduled abort work and preserves external cancellation semantics.

- [ ] **Step 4: Run state-machine tests and confirm the red state**

```bash
uv run pytest tests/test_discovery.py -v
```

Expected: collection/signature failures identify the absent `PanelStateDiscoverySession`, advance method, archive lifecycle, and v2 states.

- [ ] **Step 5: Implement correlated snapshots and raw event routing**

`_async_request_snapshot(phase)` sets a random `disc-<32 hex>` token and full-reported future, calls the only requester with a recorder that enqueues the exact outgoing event under the current `ArchiveContext`, then waits 10 seconds. Incoming observer order is:

1. enqueue the accepted target raw event when archive is enabled;
2. ignore `desired` for sanitized evidence;
3. use update/accepted `reported` only for bounded transient tracking;
4. resolve only a target get/accepted with matching token and complete target `reported` root.

Never expose the token or event on an Action result, repr, or log.

- [ ] **Step 6: Implement the catalog-driven phase engine**

Store the immutable `DiscoveryStepRequest`, a fixed step baseline, optional carrier-local baseline, the current phase index, per-phase attempt number, and completed `CycleEvidence` rows. A mode advances `MODE_ON -> MODE_RESTORE`; a parameter advances the four fixed phases; idle completes after one observation.

At restoration phases, call Task 5's path-level evaluator. If restoration is mandatory, remain on the same phase in `RESTORE_REQUIRED`. If no non-background positive change exists, record an observed-empty phase and still prompt for/record the restoration or carrier shutdown required by the physical procedure.

The state machine emits only `DiscoveryProgress`; it never writes an HA device state or constructs an assumed on/level/temperature result.

- [ ] **Step 7: Implement async abort/finalization and fixed errors**

Map the new fixed errors and use one scheduled abort path for synchronous transport/archive callbacks. Finish is legal only from `READY`: complete the archive first, build/validate/save the sanitized report second, and retain the completed raw archive if report saving fails. Archive completion failure keeps `.partial` and must not replace the old report.

Cancellation never sends restoration commands. Its response is based only on whether a positive panel phase lacks returned restoration evidence.

- [ ] **Step 8: Verify the full session**

```bash
uv run pytest tests/test_discovery.py tests/test_discovery_analysis.py tests/test_raw_discovery_archive.py -q
uv run ruff check custom_components/aupu_q360/discovery.py custom_components/aupu_q360/errors.py tests/test_discovery.py
uv run mypy custom_components/aupu_q360/discovery.py custom_components/aupu_q360/errors.py
```

Expected: pass; the only outgoing events captured by session tests are correlated Shadow gets, and every failure releases all owned async work.

- [ ] **Step 9: Commit the v2 state machine**

```bash
git add custom_components/aupu_q360/discovery.py custom_components/aupu_q360/errors.py tests/test_discovery.py
git diff --cached --check
git commit -m "feat(状态发现): 实现 Q360 面板实验状态机"
```

### Task 7: Replace the v1 Actions, Selectors, Responses, and Translations

**Files:**

- Rewrite: `custom_components/aupu_q360/services.py:1-231`
- Rewrite: `custom_components/aupu_q360/services.yaml:1-64`
- Modify: `custom_components/aupu_q360/strings.json:102-180`
- Modify: `custom_components/aupu_q360/translations/zh-Hans.json:102-180`
- Rewrite: `tests/test_services.py`

**Interfaces:**

- Registers exactly: `start_discovery`, `begin_discovery_step`, `advance_discovery_step`, `finish_discovery`, and `cancel_discovery`.
- Removes: `complete_discovery_step` without a compatibility alias.
- Consumes: Task 1 `build_step_request()` and Task 6 session methods.
- Produces: fixed response-only dictionaries; no report body, raw metadata path, token, device identifier, or exception text.

- [ ] **Step 1: Write registration/schema/response red tests**

Assert all five v2 Actions register once across multiple entries and unregister after the final entry. Assert `complete_discovery_step` is absent.

Schemas must require:

- start: `config_entry_id` and `all_modes_off_confirmed` equal to `True`;
- begin: `config_entry_id`, catalog `experiment`, round `1/2`, and only the legal optional parameter pair for that experiment;
- advance/finish/cancel: `config_entry_id` only;
- every schema rejects extra fields, including a caller-supplied phase/carrier/path/archive path.

Use a fake session to assert begin receives one `DiscoveryStepRequest`, advance receives no caller phase, and fixed progress is returned. Finish returns only classification counts, coverage counts, `report_available=True`, state, and message code. Cancel includes only the manual-restore boolean.

- [ ] **Step 2: Write YAML and translation parity red tests**

Parse `services.yaml`, `strings.json`, and `zh-Hans.json`. Require the exact ten experiment selector options, integer selectors for levels `1..5` and temperatures `30..42` with step `1`, and matching fields/action/error/message keys in both languages.

Required prompt codes include:

```text
discovery_ready_for_step
discovery_prompt_mode_on
discovery_prompt_mode_restore
discovery_prompt_carrier_on
discovery_prompt_parameter_change
discovery_prompt_parameter_restore
discovery_prompt_carrier_off
discovery_prompt_idle_observation
discovery_restore_required
discovery_manual_restore_required
discovery_cycle_recorded
discovery_report_saved
discovery_cancelled
```

Required new errors are the six v2 codes in the specification. Messages must tell the user to inspect the physical panel after restoration uncertainty; they must not claim the software turned a mode off.

- [ ] **Step 3: Run Action tests and confirm the red state**

```bash
uv run pytest tests/test_services.py tests/test_manifest.py -q
```

Expected: failures show v1 capability/target fields and `complete_discovery_step` are still registered.

- [ ] **Step 4: Implement catalog-derived Action validation**

Build the allowed experiment selector tuple from `EXPERIMENT_CATALOG`. Voluptuous accepts the six begin fields as optional after the common required fields; the handler passes all values to `build_step_request()`, which enforces the exact field matrix. Map `ValueError` only to fixed `discovery_invalid_parameter`.

Resolve a duck-typed session only if it has all six v2 methods. `_call_session()` converts `DiscoveryError.error_code` to one translatable `ServiceValidationError` without dynamic details.

- [ ] **Step 5: Implement YAML, English, and Chinese descriptions**

Describe all actions as read-only Shadow snapshot orchestration. Explicitly state that parameter fields are conditionally required, the carrier is fixed by the catalog, `advance` takes no phase, and panel operation/restoration is manual. Keep English and Chinese JSON key sets byte-for-byte equivalent after parsing.

- [ ] **Step 6: Verify Actions and translations**

```bash
uv run pytest tests/test_services.py tests/test_manifest.py -q
uv run ruff check custom_components/aupu_q360/services.py tests/test_services.py
uv run mypy custom_components/aupu_q360/services.py
uv run python -m json.tool custom_components/aupu_q360/strings.json >/dev/null
uv run python -m json.tool custom_components/aupu_q360/translations/zh-Hans.json >/dev/null
```

Expected: pass; no v1 action/label appears in service registration, selectors, or translation service keys.

- [ ] **Step 7: Commit the v2 Action surface**

```bash
git add custom_components/aupu_q360/services.py custom_components/aupu_q360/services.yaml custom_components/aupu_q360/strings.json custom_components/aupu_q360/translations/zh-Hans.json tests/test_services.py tests/test_manifest.py
git diff --cached --check
git commit -m "feat(状态发现): 切换 Q360 v2 只读操作接口"
```

### Task 8: Add the Archive Option and Wire Runtime Lifecycle

**Files:**

- Modify: `custom_components/aupu_q360/models.py:59-163`
- Modify: `custom_components/aupu_q360/config_flow.py:24-105,157-195,471-553`
- Modify: `custom_components/aupu_q360/__init__.py:14-186`
- Modify: `tests/test_config_flow.py`
- Modify: `custom_components/aupu_q360/strings.json`
- Modify: `custom_components/aupu_q360/translations/zh-Hans.json`

**Interfaces:**

- Produces persisted `raw_archive_enabled: bool`, default `False`, in `AupuConfigEntryData`.
- Preserves every existing credential/token/phone/WSS option behavior and atomic update/reload behavior.
- Replaces runtime `StateDiscoverySession` with `PanelStateDiscoverySession`.
- Produces an optional archive factory only when `raw_archive_enabled=True`; no root check or directory creation occurs at HA setup/options time.

- [ ] **Step 1: Add model/config-flow red tests**

Assert old entries without the field normalize to `False` and `as_mapping()` writes an explicit boolean. The initial config form remains archive-off and does not ask for a path. Options exposes only a boolean `raw_archive_enabled`, never a path, and preserves it across token, phone, WSS, manual reauth, and SMS reauth updates.

Reject non-boolean stored/submitted values without changing entry data. Enabling the option performs zero filesystem/network work and reloads exactly once only after a complete valid candidate. Disabling WSS may preserve the archive preference, but discovery remains transport-unavailable until WSS is re-enabled.

- [ ] **Step 2: Add runtime wiring/lifecycle red tests**

Assert setup constructs `PanelStateDiscoverySession`, validator from `discovery_report_schema`, and an archive factory bound only to the fixed container path. Enabled archive with a missing root does not fail entry setup; `start_discovery` later fails before a Shadow request. Disabled archive never calls `RawDiscoveryArchive.async_open`.

On forward failure, unload, HA stop, and external cancellation, require session/writer/coordinator stoppers to run once and runtime references to clear under the existing teardown semantics.

- [ ] **Step 3: Run config/runtime red tests**

```bash
uv run pytest tests/test_config_flow.py -q
```

Expected: failures identify the absent option field, old session class, and old validator import.

- [ ] **Step 4: Implement the boolean option without exposing a path**

Add `_CONF_RAW_ARCHIVE_ENABLED = "raw_archive_enabled"`. `AupuConfigEntryData.from_mapping()` validates `type(value) is bool`; `as_mapping()` includes it. `_parse_user_input()` supplies false. `_options_schema()` defaults from current data and `_async_update_entry()` persists the complete candidate.

Update only the Options copy:

```text
启用本机私有原始发现档案（默认关闭；只使用管理员配置的固定容器挂载）
```

Do not claim the directory exists or is writable until `start_discovery` checks it.

- [ ] **Step 5: Wire the v2 session and archive factory**

The runtime creates `DiscoverySanitizer`, v2 validator/store, and `PanelStateDiscoverySession`. The archive factory calls `RawDiscoveryArchive.async_open()` with no caller path. Observer activation remains one-per-coordinator. Session stays before coordinator in the stopper list so discovery/writer cleanup completes before WSS teardown.

Transport cancellation passes only the fixed WSS-unavailable code. HA stop calls session cancellation/stop without logging event contents.

- [ ] **Step 6: Verify config and runtime lifecycle**

```bash
uv run pytest tests/test_config_flow.py tests/test_services.py -q
uv run ruff check custom_components/aupu_q360/models.py custom_components/aupu_q360/config_flow.py custom_components/aupu_q360/__init__.py tests/test_config_flow.py
uv run mypy custom_components/aupu_q360
```

Expected: pass; existing credential, reauth, WSS, light platform, teardown, and cancellation tests remain green.

- [ ] **Step 7: Commit runtime wiring**

```bash
git add custom_components/aupu_q360/models.py custom_components/aupu_q360/config_flow.py custom_components/aupu_q360/__init__.py custom_components/aupu_q360/strings.json custom_components/aupu_q360/translations/zh-Hans.json tests/test_config_flow.py
git diff --cached --check
git commit -m "feat(状态发现): 接入私有档案选项和运行时"
```

### Task 9: Move Sanitized Persistence and Diagnostics to Schema v2

**Files:**

- Modify: `custom_components/aupu_q360/discovery_store.py:1-96`
- Modify: `custom_components/aupu_q360/diagnostics.py:1-140`
- Rewrite: `tests/test_discovery_store.py`
- Modify: `tests/test_diagnostics.py`

**Interfaces:**

- Produces: new key `aupu_q360.discovery_v2.<entry_id>` using HA Store container version `1` and schema-v2 report validation.
- Preserves: atomic/private Store save semantics and old-report-on-save-failure behavior.
- Removal policy: normal load/save never reads, rewrites, migrates, or deletes the v1 key; Config Entry deletion removes both `aupu_q360.discovery_v2.<entry_id>` and legacy `aupu_q360.discovery.<entry_id>`.
- Diagnostics produces only the validated schema-v2 report and its already-sanitized archive metadata.

- [ ] **Step 1: Write new-key and incompatibility red tests**

Assert v2 save/load uses the new key and never invokes a Store migration callback. Seed a v1 report under the legacy key and assert v2 load returns unavailable rather than relabeling it. Fail validation/save and prove the prior v2 report remains.

On Config Entry deletion, assert both exact keys are removed and no host raw archive directory is inspected or deleted. Unload continues to preserve both persisted reports.

- [ ] **Step 2: Extend Diagnostics exfiltration tests**

Load a fully valid v2 report and assert Diagnostics includes it unchanged under `state_discovery.report`. Inject raw topic/payload/path/token markers through an invalid fake Store and assert `report_available=False`, fixed logs only, and zero marker occurrence in output/caplog.

For enabled archive metadata, Diagnostics may include only session ID, status, event count, encoded byte count, and SHA-256. It must not include `/var/lib/...`, `/home/george/...`, topic, payload, Base64, device ID, or manifest content. Disabled metadata stays the two-field not-requested form.

- [ ] **Step 3: Run Store/Diagnostics red tests**

```bash
uv run pytest tests/test_discovery_store.py tests/test_diagnostics.py -q
```

Expected: failures show the v1 key is still used and current schema validation rejects version 2.

- [ ] **Step 4: Implement the new key and explicit legacy deletion policy**

Keep HA Store's wrapper version at `1` because the key is new; this avoids HA calling an undefined migration function. `_storage_key_v2(entry_id)` is used for load/save/remove. `async_remove_for_entry()` constructs two private atomic Store objects and removes the exact v2 and legacy keys, logging only fixed text if either fails.

- [ ] **Step 5: Verify persistence and Diagnostics**

```bash
uv run pytest tests/test_discovery_store.py tests/test_diagnostics.py -q
uv run ruff check custom_components/aupu_q360/discovery_store.py custom_components/aupu_q360/diagnostics.py tests/test_discovery_store.py tests/test_diagnostics.py
uv run mypy custom_components/aupu_q360/discovery_store.py custom_components/aupu_q360/diagnostics.py
```

Expected: pass; a legacy report can neither appear as v2 nor be destroyed during upgrade/setup, and Config Entry deletion still leaves raw host archives untouched.

- [ ] **Step 6: Commit v2 persistence**

```bash
git add custom_components/aupu_q360/discovery_store.py custom_components/aupu_q360/diagnostics.py tests/test_discovery_store.py tests/test_diagnostics.py
git diff --cached --check
git commit -m "feat(状态发现): 持久化 v2 脱敏报告"
```

### Task 10: Prove the Network Boundary and Home Assistant Runtime Workflow

**Files:**

- Create: `tests/test_discovery_network_boundary.py`
- Modify: `tests/ha_runtime/test_ha_runtime.py`
- Modify: `tests/test_wss.py`
- Modify: `tests/test_light.py`

**Interfaces:**

- Consumes: all production interfaces from Tasks 1-9.
- Produces: no production interface; adds integration-level proof using the real HA service registry and fake WSS/filesystem only.

- [ ] **Step 1: Add a static and dynamic discovery-network guard**

Parse `discovery.py`, `discovery_catalog.py`, `discovery_analysis.py`, `discovery_report_schema.py`, `raw_discovery_archive.py`, and `services.py` with `ast`. Reject imports/references to device control methods, `CONTROL_PATH`, `AupuApiClient`, `set_light`, Shadow update publish literals, or caller-supplied URLs/paths. Allow only the coordinator's `async_request_shadow_get` dependency. Do not apply this static import rule to `__init__.py` or `coordinator.py`, because those modules legitimately own the existing unrelated lighting API; cover their discovery route dynamically instead.

Dynamically complete a synthetic session with socket/DNS guards active and capture every MQTT publish. Assert every discovery publish topic ends in `/shadow/get`, every payload contains only one valid `clientToken`, and no mode/level/temperature/desired/update payload or HTTPS request occurs. Keep the existing initial WSS `{}` get distinguished from discovery gets.

- [ ] **Step 2: Add a real-HA Action workflow test**

Using `pytest-homeassistant-custom-component`, load a synthetic entry with fake WSS and a temporary monkeypatched fixed archive root. Through `hass.services.async_call(..., return_response=True)`, complete:

- two idle cycles;
- two reversible mode cycles, including `night_light` sharing the existing light path;
- a five-level example with source `3` and targets `1`, `2`, `4`, `5`, each twice;
- two `35 -> 36 -> 35` temperature cycles;
- finish, Diagnostics read, Config Entry unload/reload, and Config Entry removal.

The test may omit the other six mode cycles only if separate parameterized runtime cases exercise their catalog routing; unit coverage remains exhaustive. Assert responses contain fixed progress/count fields only, report schema is 2, archive hash/count metadata matches the temporary file, and no raw content appears in Diagnostics.

- [ ] **Step 3: Add failure-isolation runtime cases**

Cover enabled archive with missing mount failing before network; observer/analysis/archive enqueue exceptions after an incoming message; WSS disconnect with last light retained/stale; restoration-required retry; multiple Config Entries; unload and HA stop. In every case assert formal light `reported` was applied first, no second observer/writer/timer remains, services unregister correctly, and no control call occurs.

- [ ] **Step 4: Run runtime red tests**

```bash
uv run pytest tests/test_discovery_network_boundary.py tests/test_wss.py tests/test_light.py -q
AUPU_RUN_HA_RUNTIME=1 uv run --group ha-test pytest tests/ha_runtime -m ha_runtime -v
```

Expected before implementation adjustments: focused failures identify missing v2 Actions/archive/report fields; no test is permitted to contact a real network.

- [ ] **Step 5: Make the smallest wiring/test-helper corrections**

Adjust only production seams required for deterministic injection: a clock, archive root through the internal archive factory, queue/size limits in unit construction, and fake WSS callbacks. Do not add any user-configurable path, second connection, test-only production branch, or raw fixture file.

- [ ] **Step 6: Verify runtime, network, and regression behavior**

```bash
uv run pytest tests/test_discovery_network_boundary.py tests/test_wss.py tests/test_light.py -q
AUPU_RUN_HA_RUNTIME=1 uv run --group ha-test pytest tests/ha_runtime -m ha_runtime -v
uv run ruff check tests/test_discovery_network_boundary.py tests/ha_runtime/test_ha_runtime.py tests/test_wss.py tests/test_light.py
```

Expected: pass; all runtime fixtures are synthetic, the network guard remains active, and the light entity behavior is unchanged.

- [ ] **Step 7: Commit runtime and network proof**

```bash
git add tests/test_discovery_network_boundary.py tests/ha_runtime/test_ha_runtime.py tests/test_wss.py tests/test_light.py
git diff --cached --check
git commit -m "test(状态发现): 覆盖 v2 HA 运行时和网络边界"
```

### Task 11: Update the Runbook, Version, and Complete Linux Verification

**Files:**

- Modify: `README.md:1-120`
- Rewrite: `docs/q360-read-only-discovery-runbook.md:1-117`
- Modify: `custom_components/aupu_q360/manifest.json:11`
- Modify: `custom_components/aupu_q360/const.py:4`
- Modify: `pyproject.toml:3`
- Modify mechanically: `uv.lock`
- Modify as required by final verification: only files already named in Tasks 1-10

**Interfaces:**

- Produces: release/version `0.2.0` consistently in project, manifest, runtime constant, and lock metadata.
- Produces: an operator runbook that exactly matches the five v2 Actions, fixed paths, restore semantics, archive privacy boundary, deployment gates, and real Task 9 order.

- [ ] **Step 1: Write documentation/version consistency tests first**

Extend `tests/test_manifest.py` to require the three version sources equal `0.2.0`, `services.yaml` lists only the five v2 Actions, README/runbook name `advance_discovery_step`, and neither document instructs HA to control modes/levels/temperature.

Assert the runbook contains both fixed archive paths, permissions `0700/0600`, the 64 MiB limit, schema 2, both restoration checks, and independent authorization gates for host directory, Compose, sync, container recreation/restart, and real panel work.

- [ ] **Step 2: Run documentation consistency tests and confirm the red state**

```bash
uv run pytest tests/test_manifest.py -v
```

Expected: failures show version `0.1.1` and v1 Action/runbook terminology.

- [ ] **Step 3: Rewrite README and the operator runbook**

README continues to say only the existing light entity is supported. Its discovery section explains ten experiment labels, seven independent modes, global levels `1..5`, AI temperature `30..42`, carrier rules, reported-only evidence, optional raw archive, and no automatic entity creation.

The runbook gives exact Action calls and response gates for mode, parameter, idle, restoration retry, cancel, and finish. It requires the operator to visually confirm all modes off and restore the original level/temperature. It says Base64 is raw private data, not sanitization, and that deleting a Config Entry does not delete host archives.

Do not include real IDs, raw topics, payloads, tokens, paths to private Config Entry backups, or captured values.

- [ ] **Step 4: Bump version and refresh the lock mechanically**

Set `0.2.0` in `pyproject.toml`, `manifest.json`, and `const.py`, then run:

```bash
uv lock
git diff -- uv.lock
```

Expected: the lock change is limited to the root project version/metadata; no dependency is added or upgraded. If `uv lock` proposes dependency resolution changes, stop and investigate rather than accepting unrelated lock churn.

- [ ] **Step 5: Run focused and full Linux verification**

```bash
uv sync --locked
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy custom_components/aupu_q360
uv run python scripts/verify_private_signer.py
uv run python scripts/check_no_secrets.py
AUPU_RUN_HA_RUNTIME=1 uv run --group ha-test pytest tests/ha_runtime -m ha_runtime -v
git diff --check
```

Expected: every command passes. The suite must not access real DNS/cloud/SMS/device endpoints or read private Config Entry material.

- [ ] **Step 6: Perform the plan/spec self-audit before claiming completion**

Run exact scans:

```bash
rg -n "complete_discovery_step|\bheating\b|\bdrying\b|\bswing\b|\btimer\b|level_0|level_6" custom_components tests README.md docs/q360-read-only-discovery-runbook.md
rg -n "shadow/update|state\.desired|set_light|CONTROL_PATH|AupuApiClient" custom_components/aupu_q360/discovery*.py custom_components/aupu_q360/raw_discovery_archive.py custom_components/aupu_q360/services.py
git status --short
git diff --stat origin/main...HEAD
```

Expected: the first scan has no v1 discovery API/labels outside explicit historical documentation tests; the second shows no discovery control path; `.codegraph/` remains untracked and unstaged; only intended source/test/doc/lock files changed. Apply the writing-plans no-placeholder checklist separately and correct every match before committing.

- [ ] **Step 7: Commit the release documentation after all verification passes**

```bash
git add README.md docs/q360-read-only-discovery-runbook.md custom_components/aupu_q360/manifest.json custom_components/aupu_q360/const.py pyproject.toml uv.lock tests/test_manifest.py
git diff --cached --check
git diff --cached --stat
git commit -m "docs(状态发现): 发布 Q360 v2 运行手册"
```

## Post-Implementation Authorization Gates

These gates are not implied by completion of Task 11 or by any local commit. Re-read the current host/runtime state immediately before each gate. Do not combine discovery with credential rotation, HA upgrades, unrelated Compose cleanup, or raw archive deletion.

### Gate A: Review, Merge, and Push the Verified Commit Series

- [ ] Confirm `git status --short`, branch, HEAD, `origin/main`, commit series, and that `.codegraph/` is neither tracked nor staged.
- [ ] Re-run the complete Task 11 verification against committed HEAD.
- [ ] Obtain current authorization for merge if implementation used a worktree/branch; use `--ff-only` and reverify the resulting `main` tree.
- [ ] Obtain separate current authorization for `git push origin main`; never force-push or rewrite history.
- [ ] Compare the remote `main` object ID with local HEAD after push. A successful push does not authorize deployment.

### Gate B: Create and Verify the Host Private Root

- [ ] Obtain current authorization to create the host directory.
- [ ] Resolve the exact path and prove it is outside the repository, HA `/config`, and HA backup roots:

```bash
RAW_DISCOVERY_HOST_ROOT=/home/george/.local/state/ha-aupu-q360/raw-discovery
test ! -L "$RAW_DISCOVERY_HOST_ROOT"
install -d -m 0700 "$RAW_DISCOVERY_HOST_ROOT"
stat -c '%F %a %U:%G %n' "$RAW_DISCOVERY_HOST_ROOT"
```

- [ ] Do not list or print future archive contents. Directory creation alone does not authorize Compose changes.

### Gate C: Add and Validate the Fixed Compose Bind Mount

- [ ] Obtain current authorization to back up and edit the actual HA Compose file.
- [ ] Re-resolve the live Compose project/container/config mount; do not rely on an old path or memory.
- [ ] Preserve a mode/owner/hash-verified backup without reading credentials.
- [ ] Add exactly one read-write bind mount to the HA service:

```yaml
- /home/george/.local/state/ha-aupu-q360/raw-discovery:/var/lib/aupu-q360-private-discovery:rw
```

- [ ] Run `docker compose config --quiet`, inspect the resolved mount source/target/mode, and stop. A parsed Compose file does not authorize component synchronization or container recreation.

### Gate D: Synchronize the Exact Verified Component Allowlist

- [ ] Obtain current authorization to synchronize `custom_components/aupu_q360` into the resolved HA `/config/custom_components` location.
- [ ] Build a candidate directory from committed HEAD using only tracked files under `custom_components/aupu_q360`; exclude caches, tests, `.codegraph/`, raw archives, Config Entry material, and Git metadata.
- [ ] Record source/candidate SHA-256 manifests, retain the current live component as a recoverable same-filesystem backup, and use atomic directory renames. Do not use recursive deletion or `rsync --delete`.
- [ ] Run the HA configuration check against the candidate/live result. On failure, restore the backup and re-run the check. Stop with “files synchronized; running HA has not loaded them.”

### Gate E: Recreate/Restart HA and Perform a No-Discovery Smoke Test

- [ ] Obtain current authorization for the exact container recreation/restart required to load both the bind mount and component code.
- [ ] Reconfirm live component hashes and the resolved Compose mount immediately before recreation.
- [ ] Recreate only the Home Assistant service through its actual Compose project. Do not rebuild unrelated containers or upgrade images.
- [ ] Verify HA HTTP recovery, unchanged HA version, loaded `AUPU Q360` entry, existing light entity, WSS connectivity entity, reported/assumed/stale semantics, fixed mount visibility and mode, and absence of new fixed auth/protocol/runtime errors.
- [ ] Confirm the raw archive option is still off and no session directory/file was created. Deployment success does not authorize a real panel experiment.

### Gate F: Execute the Real Task 9 Read-Only Panel Session

- [ ] Obtain current authorization immediately before the session and confirm the user is beside the physical panel.
- [ ] In HA Options, enable raw archive only after confirming the fixed mount; this reload must not start discovery.
- [ ] Record privately, without chat/log output, the panel's original global level and AI temperature. Confirm all seven modes are off.
- [ ] Call `start_discovery` with `all_modes_off_confirmed: true`; stop on any response other than the fixed ready code.
- [ ] Complete idle rounds 1 and 2 first, waiting 15-30 seconds without panel changes.
- [ ] For each of the seven catalog mode labels, complete rounds 1 and 2: begin, turn only that mode on, advance, turn only that mode off, advance, and visually confirm restoration before the next cycle.
- [ ] With the original global level as `source_level`, test each of the other four values as `target_level` in rounds 1 and 2. For every cycle: enable ventilation, advance; change source to target, advance; restore source, advance; turn ventilation off, advance.
- [ ] With the original AI temperature as `source_temperature`, choose one legal adjacent target (`31` at source `30`, `41` at source `42`, otherwise one fixed adjacent value) and use the same direction in rounds 1 and 2. For every cycle: enable AI thermostatic warmth, advance; change temperature, advance; restore temperature, advance; turn AI warmth off, advance.
- [ ] On `discovery_restore_required`, do not begin another experiment. Correct the physical panel, call `advance_discovery_step` again, and continue only after a restoration-confirmed prompt.
- [ ] On timeout, disconnect, archive error, resource error, cancellation, or manual-restore code, stop software collection and physically check all modes, the original level, and the original temperature. Do not assume HA restored them.
- [ ] Finish only from `READY`. Verify Diagnostics schema 2 and archive metadata, then verify host session directory/file modes, manifest status, event/byte count, and SHA-256 without displaying topic/payload/Base64 content.
- [ ] Audit the session's transport test/telemetry to prove discovery emitted only Shadow gets. Do not interpret fields or design entities until the sanitized report and local raw evidence have been separately reviewed.

Existing HAR/SAZ/PCAP material is not needed for Tasks 1-11 or the normal Gate F workflow. If real Shadow `reported` evidence is absent or internally inconsistent, stop the run and ask the user whether the existing private capture may be inspected locally under a new explicit scope; never copy it into Git, Diagnostics, memory, or chat.
