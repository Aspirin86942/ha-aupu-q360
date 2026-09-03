# Q360 正式面板状态映射 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把真实 Shadow `reported` 证据转成四个正式只读 HA 面板状态实体，完整删除一次性探针，并交付经过测试、推送和本机 HA 验证的 `0.3.0`。

**Architecture:** `shadow.py` 只从目标设备的四个精确路径构造 typed partial update；`AupuCoordinator` 原子应用照明和面板状态，并按 WSS connection generation 跟踪每个字段 freshness。三个 sensor 和一个 binary sensor 只投影 Coordinator 的规范化状态；所有探针 Action、关联 get、持久化可能性和动态字段表面最终删除。

**Tech Stack:** Python 3.13.2+、Home Assistant custom integration、AWS IoT MQTT-over-WSS、pytest、pytest-homeassistant-custom-component、Ruff、mypy、uv。

**Spec:** `docs/superpowers/specs/2026-09-04-q360-formal-panel-state-mapping-design.md`

## Global Constraints

- 从包含已审阅 spec 提交 `73e6db1` 及本计划提交的基线创建独立 worktree；不得复用或改写 `/home/george/projects/python/ha-aupu-q360-ablation`。
- Python 下限保持 `>=3.13.2`，不增加生产依赖或开发依赖。
- 只消费目标 thing 的 `state.reported`；面板字段不得读取 `desired`，不得遍历或保存其他路径。
- 唯一正式路径是 `3/2` 当前模式、`6/4` 小夜灯、`6/5` 风量、`3/3` AI 目标温度。
- 模式映射固定为 `0/18/21/7/2/9/4`；未识别整数只能成为 `unknown`。
- `service/6/property/1`、`service/4/property/1` 和 `service/6/property/23` 不得成为实体、属性或 Diagnostics 字段。
- 不新增模式、夜灯、档位、温度控制；不调用新的 HTTPS 控制，不发布 Shadow `update`，不写 `desired`，不创建第二条 WSS。
- WSS 建连后的空 `{}` Shadow `get` 必须保留；探针专用 token get 和 transport renewal API 必须删除。
- 四个面板实体只在 WSS 配置中存在；字段未由当前连接确认、值无效或 WSS 断线时实体不可用。
- partial update 缺失字段保留；存在但无效的字段只清空自身，不阻断同一消息中的合法字段。
- 任何错误、日志、实体、Diagnostics 和测试输出都不得包含真实 device ID、tag、JWT、HA token、topic、payload 或未知原始模式整数。
- 历史 `docs/superpowers/specs/` 与 `docs/superpowers/plans/` 保留；临时探针运行手册必须删除。
- 严格执行 red-green TDD；每个任务只提交列明文件，并在提交前运行该任务的定向测试与 `git diff --check`。
- `.codegraph/`、已有其他 worktree、HA 历史备份和无关工作树改动不得修改。

## File Structure

```text
custom_components/aupu_q360/
├── __init__.py           # 构造 Coordinator，只转发 light/binary_sensor/sensor
├── binary_sensor.py      # connectivity 与只读 night-light 实体
├── const.py              # 0.3.0 版本常量
├── coordinator.py        # 原子状态应用、每字段 freshness、listener fanout
├── diagnostics.py        # 固定规范化状态白名单
├── entity.py             # 新增：四个面板实体共享的设备/listener 基类
├── manifest.json         # 0.3.0 集成版本
├── models.py             # runtime 不再拥有 probe
├── sensor.py             # 新增：mode、fan level、AI target temperature
├── shadow.py             # typed reported-only PanelStateUpdate
├── strings.json          # 正式实体名与 mode state 翻译源
├── translations/zh-Hans.json
│                         # 简体中文实体名与 mode state 翻译
└── wss.py                # 保留单 WSS 与空 get；删除探针 get/renewal

tests/
├── test_coordinator.py   # 新增：面板状态、freshness、原子通知
├── test_sensor.py        # 新增：三个 sensor 与 HTTPS-only 清理
├── test_binary_sensor.py # 增加 night-light 与双 registry 清理
├── test_shadow.py        # 精确映射、partial、unknown/invalid
├── test_diagnostics.py   # 正式规范化状态白名单
├── test_manifest.py      # 无 probe 表面、翻译结构、0.3.0
├── test_wss.py           # 只保留初始空 get 的网络边界
└── ha_runtime/test_ha_runtime.py
                          # 真实 HA 实体注册、更新、reload、unload
```

最终删除：

```text
custom_components/aupu_q360/probe.py
custom_components/aupu_q360/services.py
custom_components/aupu_q360/services.yaml
docs/q360-read-only-discovery-runbook.md
tests/test_probe.py
tests/test_probe_network_boundary.py
tests/test_services.py
```

---

### Task 1: 解析四个正式 reported 字段

**Files:**
- Modify: `custom_components/aupu_q360/shadow.py:1-136`
- Modify: `tests/test_shadow.py:1-147`

**Interfaces:**
- Consumes: `DeviceConfig.did`、已校验的 `AcceptedShadow(topic_kind, state, client_token)`。
- Produces: `PanelMode`、`PANEL_MODE_OPTIONS`、`PanelFieldUpdate[T]`、`PanelStateUpdate`、`parse_panel_shadow_update(device, message) -> PanelStateUpdate | None`。
- Intermediate constraint: 本任务暂时保留 `AcceptedShadow.client_token`，以便现有 probe 测试在 Task 4 删除前继续通过。

- [ ] **Step 1: 写入模式与完整快照失败测试**

在 `tests/test_shadow.py` 增加以下 import、helper 和参数测试：

```python
from custom_components.aupu_q360.shadow import (
    AcceptedShadow,
    PanelFieldUpdate,
    PanelStateUpdate,
    parse_panel_shadow_update,
)


def _panel_message(
    *,
    service_3: dict[str, object] | None = None,
    service_6: dict[str, object] | None = None,
    section: str = "reported",
) -> AcceptedShadow:
    device_state: dict[str, object] = {}
    if service_3 is not None:
        device_state["3"] = {"properties": service_3}
    if service_6 is not None:
        device_state["6"] = {"properties": service_6}
    return AcceptedShadow(
        topic_kind="update",
        state={section: {DEVICE.did: device_state}},
    )


@pytest.mark.parametrize(
    ("raw_mode", "expected"),
    [
        (0, "off"),
        (18, "ai_thermostatic_warmth"),
        (21, "deodorization_sterilization"),
        (7, "ventilation"),
        (2, "air_blowing"),
        (9, "normal_drying"),
        (4, "thermostatic_drying"),
        (999, "unknown"),
    ],
)
def test_panel_mode_uses_only_confirmed_mapping(raw_mode: int, expected: str) -> None:
    update = parse_panel_shadow_update(
        DEVICE,
        _panel_message(service_3={"2": raw_mode}),
    )

    assert update is not None
    assert update.mode == PanelFieldUpdate(present=True, value=expected)
    assert update.night_light == PanelFieldUpdate(present=False, value=None)
    assert update.fan_level == PanelFieldUpdate(present=False, value=None)
    assert update.ai_target_temperature == PanelFieldUpdate(present=False, value=None)


def test_panel_full_reported_snapshot_is_normalized() -> None:
    update = parse_panel_shadow_update(
        DEVICE,
        _panel_message(
            service_3={"2": 7, "3": 36},
            service_6={"4": False, "5": 5},
        ),
    )

    assert update == PanelStateUpdate(
        mode=PanelFieldUpdate(present=True, value="ventilation"),
        night_light=PanelFieldUpdate(present=True, value=False),
        fan_level=PanelFieldUpdate(present=True, value=5),
        ai_target_temperature=PanelFieldUpdate(present=True, value=36),
    )
```

- [ ] **Step 2: 运行测试并确认 red**

Run:

```bash
uv run pytest -q \
  tests/test_shadow.py::test_panel_mode_uses_only_confirmed_mapping \
  tests/test_shadow.py::test_panel_full_reported_snapshot_is_normalized
```

Expected: collection fails because the five panel parser interfaces do not yet exist.

- [ ] **Step 3: 写入 partial、desired 和无效值失败测试**

```python
def test_panel_partial_update_distinguishes_missing_from_invalid() -> None:
    update = parse_panel_shadow_update(
        DEVICE,
        _panel_message(
            service_3={"2": "7", "3": 29},
            service_6={"4": 1, "5": 6},
        ),
    )

    assert update == PanelStateUpdate(
        mode=PanelFieldUpdate(present=True, value=None),
        night_light=PanelFieldUpdate(present=True, value=None),
        fan_level=PanelFieldUpdate(present=True, value=None),
        ai_target_temperature=PanelFieldUpdate(present=True, value=None),
    )


def test_panel_desired_and_unrelated_reported_paths_are_ignored() -> None:
    desired = parse_panel_shadow_update(
        DEVICE,
        _panel_message(service_3={"2": 18, "3": 36}, section="desired"),
    )
    unrelated = parse_panel_shadow_update(
        DEVICE,
        AcceptedShadow(
            topic_kind="update",
            state={
                "reported": {
                    DEVICE.did: {
                        "4": {"properties": {"1": 35}},
                        "6": {"properties": {"1": 90, "23": 1}},
                    }
                }
            },
        ),
    )

    assert desired is None
    assert unrelated is None


@pytest.mark.parametrize(
    ("service_3", "service_6"),
    [
        ({"3": 30}, {"5": 1}),
        ({"3": 42}, {"5": 5}),
    ],
)
def test_panel_numeric_boundaries_are_inclusive(
    service_3: dict[str, object],
    service_6: dict[str, object],
) -> None:
    update = parse_panel_shadow_update(
        DEVICE,
        _panel_message(service_3=service_3, service_6=service_6),
    )

    assert update is not None
    assert update.ai_target_temperature.value == service_3["3"]
    assert update.fan_level.value == service_6["5"]
```

- [ ] **Step 4: 实现 typed partial parser**

在 `shadow.py` 的 `AcceptedShadow` 后增加以下类型和常量：

```python
type PanelMode = Literal[
    "off",
    "ai_thermostatic_warmth",
    "deodorization_sterilization",
    "ventilation",
    "air_blowing",
    "normal_drying",
    "thermostatic_drying",
    "unknown",
]

PANEL_MODE_OPTIONS: tuple[PanelMode, ...] = (
    "off",
    "ai_thermostatic_warmth",
    "deodorization_sterilization",
    "ventilation",
    "air_blowing",
    "normal_drying",
    "thermostatic_drying",
    "unknown",
)

_MODE_BY_VALUE: dict[int, PanelMode] = {
    0: "off",
    18: "ai_thermostatic_warmth",
    21: "deodorization_sterilization",
    7: "ventilation",
    2: "air_blowing",
    9: "normal_drying",
    4: "thermostatic_drying",
}
_MISSING = object()


@dataclass(frozen=True, slots=True)
class PanelFieldUpdate[T]:
    """One reported field, distinguishing omission from an unusable value."""

    present: bool
    value: T | None


@dataclass(frozen=True, slots=True)
class PanelStateUpdate:
    """Only the four confirmed Q360 panel paths from one reported message."""

    mode: PanelFieldUpdate[PanelMode]
    night_light: PanelFieldUpdate[bool]
    fan_level: PanelFieldUpdate[int]
    ai_target_temperature: PanelFieldUpdate[int]
```

同时从 `collections.abc` 导入 `Callable`，并增加精确读取和归一化函数：

```python
def parse_panel_shadow_update(
    device: DeviceConfig,
    message: AcceptedShadow,
) -> PanelStateUpdate | None:
    """Parse only confirmed panel fields from the target reported state."""
    reported = message.state.get("reported")
    if reported is None:
        return None
    if not isinstance(reported, dict):
        raise AupuProtocolError
    device_state = reported.get(device.did)
    if device_state is None:
        return None
    if not isinstance(device_state, dict):
        raise AupuProtocolError

    update = PanelStateUpdate(
        mode=_field_update(
            _panel_property(device_state, "3", "2"),
            _normalize_mode,
        ),
        night_light=_field_update(
            _panel_property(device_state, "6", "4"),
            _normalize_bool,
        ),
        fan_level=_field_update(
            _panel_property(device_state, "6", "5"),
            lambda value: _normalize_bounded_int(value, minimum=1, maximum=5),
        ),
        ai_target_temperature=_field_update(
            _panel_property(device_state, "3", "3"),
            lambda value: _normalize_bounded_int(value, minimum=30, maximum=42),
        ),
    )
    return update if any(
        field.present
        for field in (
            update.mode,
            update.night_light,
            update.fan_level,
            update.ai_target_temperature,
        )
    ) else None


def _panel_property(
    device_state: dict[str, Any],
    service_id: str,
    property_id: str,
) -> object:
    service = device_state.get(service_id, _MISSING)
    if service is _MISSING:
        return _MISSING
    if not isinstance(service, dict):
        raise AupuProtocolError
    properties = service.get("properties", _MISSING)
    if properties is _MISSING:
        return _MISSING
    if not isinstance(properties, dict):
        raise AupuProtocolError
    return properties.get(property_id, _MISSING)


def _field_update[T](
    raw_value: object,
    normalize: Callable[[object], T | None],
) -> PanelFieldUpdate[T]:
    if raw_value is _MISSING:
        return PanelFieldUpdate(present=False, value=None)
    return PanelFieldUpdate(present=True, value=normalize(raw_value))


def _normalize_mode(value: object) -> PanelMode | None:
    if type(value) is not int:
        return None
    return _MODE_BY_VALUE.get(value, "unknown")


def _normalize_bool(value: object) -> bool | None:
    return value if type(value) is bool else None


def _normalize_bounded_int(value: object, *, minimum: int, maximum: int) -> int | None:
    return value if type(value) is int and minimum <= value <= maximum else None
```

- [ ] **Step 5: 运行 Shadow 测试并确认 green**

Run: `uv run pytest -q tests/test_shadow.py`

Expected: all Shadow tests pass; existing light and temporary client-token behavior remain unchanged.

- [ ] **Step 6: 检查 diff 并提交**

```bash
git diff --check
git diff -- custom_components/aupu_q360/shadow.py tests/test_shadow.py
git add custom_components/aupu_q360/shadow.py tests/test_shadow.py
git commit -m "feat(状态映射): 解析正式 reported 字段"
```

---

### Task 2: Coordinator 原子应用状态并跟踪 freshness

**Files:**
- Create: `tests/test_coordinator.py`
- Modify: `custom_components/aupu_q360/coordinator.py:1-327`

**Interfaces:**
- Consumes: Task 1 的 `PanelFieldUpdate[T]`、`PanelStateUpdate`、`PanelMode` 和 `parse_panel_shadow_update()`。
- Produces: `panel_mode`、`night_light_is_on`、`fan_level`、`ai_target_temperature` 值属性，同名 `_available` 属性及 `panel_state_available`。
- Intermediate constraint: 在 Task 4 以前，probe observer 仍在正式照明和面板状态应用之后收到消息。

- [ ] **Step 1: 写入状态、partial 和原子通知失败测试**

创建 `tests/test_coordinator.py`，复用合成 `DeviceConfig`、`BearerCredential` 和最小 Fake API，增加：

```python
def test_shadow_message_applies_light_and_panel_before_one_notification() -> None:
    coordinator = _coordinator()
    observed: list[tuple[bool | None, str | None, int | None]] = []
    coordinator.async_add_listener(
        lambda: observed.append(
            (coordinator.is_on, coordinator.panel_mode, coordinator.fan_level)
        )
    )
    coordinator.async_apply_wss_connection(connected=True, healthy=False)
    observed.clear()

    coordinator.async_apply_shadow_message(
        AcceptedShadow(
            topic_kind="get",
            state={
                "reported": {
                    DEVICE.did: {
                        "2": {"properties": {"1": False}},
                        "3": {"properties": {"2": 7, "3": 36}},
                        "6": {"properties": {"4": False, "5": 5}},
                    }
                }
            },
        )
    )

    assert observed == [(False, "ventilation", 5)]
    assert coordinator.night_light_is_on is False
    assert coordinator.ai_target_temperature == 36
    assert coordinator.panel_mode_available is True
    assert coordinator.night_light_available is True
    assert coordinator.fan_level_available is True
    assert coordinator.ai_target_temperature_available is True


def test_partial_panel_update_preserves_missing_and_clears_only_invalid() -> None:
    coordinator = _coordinator_with_confirmed_panel_state()

    coordinator.async_apply_shadow_message(
        AcceptedShadow(
            topic_kind="update",
            state={
                "reported": {
                    DEVICE.did: {
                        "3": {"properties": {"3": 29}},
                        "6": {"properties": {"4": True}},
                    }
                }
            },
        )
    )

    assert coordinator.panel_mode == "off"
    assert coordinator.fan_level == 5
    assert coordinator.night_light_is_on is True
    assert coordinator.ai_target_temperature is None
    assert coordinator.panel_mode_available is True
    assert coordinator.ai_target_temperature_available is False
```

该新测试文件的 imports 与 helper 使用以下实际内容；`_coordinator_with_confirmed_panel_state()`
先设 connection 为 connected，再应用完整 `get`：

```python
import base64
import json
from datetime import UTC, datetime, timedelta
from typing import cast

from homeassistant.core import HomeAssistant

from custom_components.aupu_q360.api import AupuApiClient
from custom_components.aupu_q360.auth import BearerCredential
from custom_components.aupu_q360.coordinator import AupuCoordinator
from custom_components.aupu_q360.models import DeviceConfig
from custom_components.aupu_q360.shadow import AcceptedShadow

DEVICE = DeviceConfig(did="123", tag="synthetic")


def _coordinator() -> AupuCoordinator:
    payload = json.dumps(
        {"exp": int((datetime.now(UTC) + timedelta(days=2)).timestamp())}
    ).encode()
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    credential = BearerCredential.parse(f"e30.{encoded}.signature")
    hass = type("FakeRepairHass", (), {"data": {}})()
    return AupuCoordinator(
        hass=cast(HomeAssistant, hass),
        entry_id="synthetic-entry",
        credential=credential,
        api=cast(AupuApiClient, object()),
        async_request_reauth=lambda: None,
        device=DEVICE,
    )


def _coordinator_with_confirmed_panel_state() -> AupuCoordinator:
    coordinator = _coordinator()
    coordinator.async_apply_wss_connection(connected=True, healthy=False)
    coordinator.async_apply_shadow_message(
        AcceptedShadow(
            topic_kind="get",
            state={
                "reported": {
                    DEVICE.did: {
                        "3": {"properties": {"2": 0, "3": 36}},
                        "6": {"properties": {"4": False, "5": 5}},
                    }
                }
            },
        )
    )
    return coordinator
```

- [ ] **Step 2: 运行测试并确认 red**

Run: `uv run pytest -q tests/test_coordinator.py`

Expected: fails because Coordinator has no panel state properties and notifies before atomic state is complete.

- [ ] **Step 3: 实现每字段值与 freshness**

在 `coordinator.py` 增加 import 和私有 state slot：

```python
from dataclasses import dataclass

from .shadow import (
    AcceptedShadow,
    LightShadowUpdate,
    PanelFieldUpdate,
    PanelMode,
    PanelStateUpdate,
    parse_accepted_shadow,
    parse_light_shadow_update,
    parse_panel_shadow_update,
)


@dataclass(slots=True)
class _PanelFieldState[T]:
    value: T | None = None
    fresh: bool = False

    def apply(self, update: PanelFieldUpdate[T]) -> bool:
        if not update.present:
            return False
        self.value = update.value
        self.fresh = update.value is not None
        return True

    def mark_stale(self) -> None:
        self.fresh = False
```

在 `AupuCoordinator.__init__()` 初始化：

```python
self._panel_mode = _PanelFieldState[PanelMode]()
self._night_light = _PanelFieldState[bool]()
self._fan_level = _PanelFieldState[int]()
self._ai_target_temperature = _PanelFieldState[int]()
```

增加只读属性，四个 availability 都使用同一个 helper：

```python
@property
def panel_mode(self) -> PanelMode | None:
    return self._panel_mode.value

@property
def night_light_is_on(self) -> bool | None:
    return self._night_light.value

@property
def fan_level(self) -> int | None:
    return self._fan_level.value

@property
def ai_target_temperature(self) -> int | None:
    return self._ai_target_temperature.value

def _panel_field_available[T](self, field: _PanelFieldState[T]) -> bool:
    return self._wss_connected and field.fresh and field.value is not None

@property
def panel_mode_available(self) -> bool:
    return self._panel_field_available(self._panel_mode)

@property
def night_light_available(self) -> bool:
    return self._panel_field_available(self._night_light)

@property
def fan_level_available(self) -> bool:
    return self._panel_field_available(self._fan_level)

@property
def ai_target_temperature_available(self) -> bool:
    return self._panel_field_available(self._ai_target_temperature)

@property
def panel_state_available(self) -> bool:
    return any(
        (
            self.panel_mode_available,
            self.night_light_available,
            self.fan_level_available,
            self.ai_target_temperature_available,
        )
    )
```

- [ ] **Step 4: 把 Shadow 消息应用改为单次 listener fanout**

提取不通知的 `_apply_light_state()`、统一 `_notify_listeners()`，保持现有 public callback 语义：

```python
def _notify_listeners(self) -> None:
    for listener in tuple(self._listeners):
        listener()

def _apply_panel_state_update(self, update: PanelStateUpdate) -> bool:
    changed = False
    changed |= self._panel_mode.apply(update.mode)
    changed |= self._night_light.apply(update.night_light)
    changed |= self._fan_level.apply(update.fan_level)
    changed |= self._ai_target_temperature.apply(update.ai_target_temperature)
    return changed

@callback
def async_apply_shadow_message(self, message: AcceptedShadow) -> None:
    if self._device is None:
        return
    light_update = parse_light_shadow_update(self._device, message)
    panel_update = parse_panel_shadow_update(self._device, message)
    applied = False
    if light_update is not None:
        self._apply_light_state(is_on=light_update.is_on, source=light_update.source)
        applied = True
    if panel_update is not None:
        applied |= self._apply_panel_state_update(panel_update)
    if applied:
        self._notify_listeners()

    observer = self._probe_observer
    if observer is not None:
        try:
            observer(message)
        except Exception:  # noqa: BLE001 - temporary probe isolation until Task 4
            _LOGGER.error("AUPU probe observer failed")
```

`async_apply_light_state()` 调用 `_apply_light_state()` 后只调用一次 `_notify_listeners()`；
`async_apply_shadow_update()` 继续通过该 public 方法通知。

- [ ] **Step 5: 写入断线与重连 freshness 失败测试**

```python
def test_disconnect_retains_values_but_reconnect_requires_current_reported() -> None:
    coordinator = _coordinator_with_confirmed_panel_state()

    coordinator.async_apply_wss_connection(connected=False, healthy=False)
    assert coordinator.panel_mode == "off"
    assert coordinator.fan_level == 5
    assert coordinator.panel_state_available is False

    coordinator.async_apply_wss_connection(connected=True, healthy=False)
    assert coordinator.panel_mode_available is False
    assert coordinator.fan_level_available is False

    coordinator.async_apply_shadow_message(
        AcceptedShadow(
            topic_kind="get",
            state={"reported": {DEVICE.did: {"6": {"properties": {"5": 4}}}}},
        )
    )
    assert coordinator.fan_level == 4
    assert coordinator.fan_level_available is True
    assert coordinator.panel_mode == "off"
    assert coordinator.panel_mode_available is False
```

在 `async_apply_wss_connection(connected=False, ...)` 和 `async_stop()` 中调用：

```python
def _mark_panel_state_stale(self) -> None:
    self._panel_mode.mark_stale()
    self._night_light.mark_stale()
    self._fan_level.mark_stale()
    self._ai_target_temperature.mark_stale()
```

- [ ] **Step 6: 运行 Coordinator 与现有灯光测试**

Run:

```bash
uv run pytest -q tests/test_coordinator.py tests/test_light.py tests/test_shadow.py
```

Expected: all pass; existing light desired/reported semantics and probe observer isolation remain green.

- [ ] **Step 7: 检查 diff 并提交**

```bash
git diff --check
git diff -- custom_components/aupu_q360/coordinator.py tests/test_coordinator.py
git add custom_components/aupu_q360/coordinator.py tests/test_coordinator.py
git commit -m "feat(状态映射): 协调正式面板状态"
```

---

### Task 3: 创建四个 WSS-only 实体并扩展 Diagnostics

**Files:**
- Create: `custom_components/aupu_q360/entity.py`
- Create: `custom_components/aupu_q360/sensor.py`
- Create: `tests/test_sensor.py`
- Modify: `custom_components/aupu_q360/binary_sensor.py:1-77`
- Modify: `custom_components/aupu_q360/__init__.py:22`
- Modify: `custom_components/aupu_q360/diagnostics.py:1-115`
- Modify: `custom_components/aupu_q360/strings.json:94-101`
- Modify: `custom_components/aupu_q360/translations/zh-Hans.json:94-101`
- Modify: `tests/test_binary_sensor.py:60-210`
- Modify: `tests/test_diagnostics.py:20-130`
- Modify: `tests/test_manifest.py:170-230`

**Interfaces:**
- Consumes: Task 2 的四个值属性、四个 `_available` 属性、`panel_state_available` 和 listener API。
- Produces: `AupuPanelEntity`、`AupuCurrentModeSensor`、`AupuFanLevelSensor`、`AupuAiTargetTemperatureSensor`、`AupuNightLightBinarySensor`。
- Entity unique IDs: `<entry unique id>_current_mode`、`_fan_level`、`_ai_target_temperature`、`_night_light`。

- [ ] **Step 1: 写入三个 sensor 失败测试**

创建 `tests/test_sensor.py`，用现有测试 helper 风格构造 Coordinator，增加：

```python
def test_wss_setup_adds_three_read_only_panel_sensors() -> None:
    coordinator = _confirmed_coordinator()
    entry = SimpleNamespace(
        entry_id="synthetic-entry",
        unique_id="synthetic-unique-id",
        runtime_data=SimpleNamespace(use_wss=True, coordinator=coordinator),
    )
    entities: list[object] = []

    _run(async_setup_sensor(cast(HomeAssistant, object()), entry, entities.extend))

    assert [entity.unique_id for entity in entities] == [
        "synthetic-unique-id_current_mode",
        "synthetic-unique-id_fan_level",
        "synthetic-unique-id_ai_target_temperature",
    ]
    mode, fan, temperature = entities
    assert mode.native_value == "ventilation"
    assert mode.options == list(PANEL_MODE_OPTIONS)
    assert fan.native_value == 5
    assert fan.native_unit_of_measurement == "档"
    assert temperature.native_value == 36
    assert temperature.device_class is SensorDeviceClass.TEMPERATURE
    assert temperature.native_unit_of_measurement == UnitOfTemperature.CELSIUS
    assert all(entity.available for entity in entities)
```

`tests/test_sensor.py` 的 helper 使用以下实际内容：

```python
DEVICE = DeviceConfig(did="123", tag="synthetic")


def _confirmed_coordinator() -> AupuCoordinator:
    payload = json.dumps(
        {"exp": int((datetime.now(UTC) + timedelta(days=2)).timestamp())}
    ).encode()
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    credential = BearerCredential.parse(f"e30.{encoded}.signature")
    hass = type("FakeRepairHass", (), {"data": {}})()
    coordinator = AupuCoordinator(
        hass=cast(HomeAssistant, hass),
        entry_id="synthetic-entry",
        credential=credential,
        api=cast(AupuApiClient, object()),
        async_request_reauth=lambda: None,
        device=DEVICE,
    )
    coordinator.async_apply_wss_connection(connected=True, healthy=False)
    coordinator.async_apply_shadow_message(
        AcceptedShadow(
            topic_kind="get",
            state={
                "reported": {
                    DEVICE.did: {
                        "3": {"properties": {"2": 7, "3": 36}},
                        "6": {"properties": {"4": False, "5": 5}},
                    }
                }
            },
        )
    )
    return coordinator
```

该文件对应导入 `base64`、`json`、`UTC/datetime/timedelta`、`SimpleNamespace`、`cast`、HA sensor
常量，以及上面出现的集成类型；不得从另一个 test module 导入私有 helper。

另加以下 HTTPS-only registry 测试，精确断言三个 unique ID 都用 `SENSOR_DOMAIN` 查找并从
registry 与 `hass.states` 删除：

```python
def test_https_only_setup_removes_all_prior_panel_sensor_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lookups: list[tuple[str, str, str]] = []
    registry_removed: list[str] = []
    state_removed: list[str] = []

    class FakeRegistry:
        def async_get_entity_id(
            self, domain: str, platform: str, unique_id: str
        ) -> str:
            lookups.append((domain, platform, unique_id))
            return f"sensor.{unique_id}"

        def async_remove(self, entity_id: str) -> None:
            registry_removed.append(entity_id)

    monkeypatch.setattr(er, "async_get", lambda _: FakeRegistry())
    hass = SimpleNamespace(states=SimpleNamespace(async_remove=state_removed.append))
    entry = SimpleNamespace(
        entry_id="synthetic-entry",
        unique_id="synthetic-unique-id",
        runtime_data=SimpleNamespace(
            use_wss=False,
            coordinator=_confirmed_coordinator(),
        ),
    )
    entities: list[object] = []

    _run(async_setup_sensor(cast(HomeAssistant, hass), entry, entities.extend))

    expected_unique_ids = [
        "synthetic-unique-id_current_mode",
        "synthetic-unique-id_fan_level",
        "synthetic-unique-id_ai_target_temperature",
    ]
    assert entities == []
    assert lookups == [
        (SENSOR_DOMAIN, DOMAIN, unique_id) for unique_id in expected_unique_ids
    ]
    assert registry_removed == [f"sensor.{unique_id}" for unique_id in expected_unique_ids]
    assert state_removed == registry_removed
```

- [ ] **Step 2: 运行 sensor 测试并确认 red**

Run: `uv run pytest -q tests/test_sensor.py`

Expected: collection fails because `custom_components.aupu_q360.sensor` does not exist.

- [ ] **Step 3: 创建共享面板实体基类**

`custom_components/aupu_q360/entity.py` 内容：

```python
"""Shared lifecycle for read-only AUPU panel-state entities."""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .coordinator import AupuCoordinator


class AupuPanelEntity(Entity):
    """Attach one read-only entity to the shared coordinator and device."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        *,
        coordinator: AupuCoordinator,
        entry_id: str,
        unique_id: str,
    ) -> None:
        self._coordinator = coordinator
        self._remove_listener: Callable[[], None] | None = None
        self._attr_unique_id = unique_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            manufacturer="AUPU",
            model="Q360T5-Pro",
            name="AUPU Q360T5-Pro",
        )

    async def async_added_to_hass(self) -> None:
        self._remove_listener = self._coordinator.async_add_listener(
            self.async_write_ha_state
        )

    async def async_will_remove_from_hass(self) -> None:
        if self._remove_listener is not None:
            self._remove_listener()
            self._remove_listener = None
```

- [ ] **Step 4: 创建 sensor 平台**

`sensor.py` 必须：

```python
"""Read-only formal Q360 panel-state sensors."""

from __future__ import annotations

from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .entity import AupuPanelEntity
from .models import AupuRuntimeData
from .shadow import PANEL_MODE_OPTIONS, PanelMode

_SUFFIXES = ("current_mode", "fan_level", "ai_target_temperature")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry[AupuRuntimeData],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    base = entry.unique_id or entry.entry_id
    if not entry.runtime_data.use_wss:
        registry = er.async_get(hass)
        for suffix in _SUFFIXES:
            entity_id = registry.async_get_entity_id(
                SENSOR_DOMAIN, DOMAIN, f"{base}_{suffix}"
            )
            if entity_id is not None:
                registry.async_remove(entity_id)
                hass.states.async_remove(entity_id)
        return
    coordinator = entry.runtime_data.coordinator
    async_add_entities(
        [
            AupuCurrentModeSensor(
                coordinator=coordinator,
                entry_id=entry.entry_id,
                unique_id=f"{base}_current_mode",
            ),
            AupuFanLevelSensor(
                coordinator=coordinator,
                entry_id=entry.entry_id,
                unique_id=f"{base}_fan_level",
            ),
            AupuAiTargetTemperatureSensor(
                coordinator=coordinator,
                entry_id=entry.entry_id,
                unique_id=f"{base}_ai_target_temperature",
            ),
        ]
    )
```

三个 class 都按以下固定投影实现；mode 的 `_attr_options = list(PANEL_MODE_OPTIONS)`，fan 的
`_attr_native_unit_of_measurement = "档"`，temperature 的 `_attr_device_class` 为
`SensorDeviceClass.TEMPERATURE` 且单位为 `UnitOfTemperature.CELSIUS`：

```python
class AupuCurrentModeSensor(AupuPanelEntity, SensorEntity):
    _attr_translation_key = "current_mode"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(PANEL_MODE_OPTIONS)

    @property
    def native_value(self) -> PanelMode | None:
        return self._coordinator.panel_mode

    @property
    def available(self) -> bool:
        return self._coordinator.panel_mode_available


class AupuFanLevelSensor(AupuPanelEntity, SensorEntity):
    _attr_translation_key = "fan_level"
    _attr_native_unit_of_measurement = "档"

    @property
    def native_value(self) -> int | None:
        return self._coordinator.fan_level

    @property
    def available(self) -> bool:
        return self._coordinator.fan_level_available


class AupuAiTargetTemperatureSensor(AupuPanelEntity, SensorEntity):
    _attr_translation_key = "ai_target_temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    @property
    def native_value(self) -> int | None:
        return self._coordinator.ai_target_temperature

    @property
    def available(self) -> bool:
        return self._coordinator.ai_target_temperature_available
```

把 `Platform.SENSOR` 加到 `__init__.py` 的 `_PLATFORMS`。

- [ ] **Step 5: 写入并实现 night-light binary sensor**

在 `tests/test_binary_sensor.py` 先断言 WSS setup 返回 connectivity 和 night light，night light 的
unique ID 为 `_night_light`、translation key 为 `night_light`、没有 device class、值与
Coordinator 一致，并在断线后 unavailable。运行该单测确认失败。

测试主体使用 Task 3 的完整 reported helper：

```python
def test_wss_setup_adds_connectivity_and_night_light() -> None:
    coordinator = _confirmed_coordinator()
    entry = SimpleNamespace(
        entry_id="synthetic-entry",
        unique_id="synthetic-unique-id",
        runtime_data=SimpleNamespace(use_wss=True, coordinator=coordinator),
    )
    entities: list[BinarySensorEntity] = []

    _run(
        async_setup_binary_sensor(
            cast(HomeAssistant, object()), entry, entities.extend
        )
    )

    assert [entity.unique_id for entity in entities] == [
        "synthetic-unique-id_state_channel",
        "synthetic-unique-id_night_light",
    ]
    night_light = entities[1]
    assert night_light.translation_key == "night_light"
    assert night_light.device_class is None
    assert night_light.is_on is False
    assert night_light.available is True
    coordinator.async_apply_wss_connection(connected=False, healthy=False)
    assert night_light.available is False
```

在现有 `tests/test_binary_sensor.py::_coordinator()` 构造 Coordinator 时增加
`device=DeviceConfig(did="123", tag="synthetic")`，再增加本文件自己的
`_confirmed_coordinator()`：先调用 `_coordinator()`，设 connection connected，应用与
`tests/test_sensor.py` 相同的四字段完整 reported message并返回。对应增加 `DeviceConfig` 与
`AcceptedShadow` imports，不从其他 test module 导入 helper。

然后在 `binary_sensor.py` 增加：

```python
class AupuNightLightBinarySensor(AupuPanelEntity, BinarySensorEntity):
    """Expose the reported night-light flag without a control surface."""

    _attr_translation_key = "night_light"

    @property
    def is_on(self) -> bool | None:
        return self._coordinator.night_light_is_on

    @property
    def available(self) -> bool:
        return self._coordinator.night_light_available
```

WSS setup 同时添加 `AupuStateChannelBinarySensor` 与该实体；HTTPS-only 分支分别清理
`_state_channel` 和 `_night_light` 两个 unique ID。不得让 connectivity 的 availability 依赖面板值。

- [ ] **Step 6: 添加实体翻译并锁定 key tree**

在英文 `entity` 中保留 `state_channel` 并增加：

```json
"night_light": {"name": "Night light"}
```

增加 `sensor`：

```json
"sensor": {
  "current_mode": {
    "name": "Current mode",
    "state": {
      "off": "Off",
      "ai_thermostatic_warmth": "AI thermostatic warmth",
      "deodorization_sterilization": "Deodorization and sterilization",
      "ventilation": "Ventilation",
      "air_blowing": "Air blowing",
      "normal_drying": "Normal drying",
      "thermostatic_drying": "Thermostatic drying",
      "unknown": "Unknown"
    }
  },
  "fan_level": {"name": "Fan level"},
  "ai_target_temperature": {"name": "AI target temperature"}
}
```

简体中文使用完全相同 key tree，显示文本分别为“小夜灯”“当前运行模式”“关闭”“AI 恒温暖”
“除臭除菌”“换气”“吹风”“普通干燥”“恒温干燥”“未知”“风量档位”“AI 目标温度”。

修改 `test_manifest.py` 精确断言两种语言 key tree 相同及上述 entity keys/state keys 完整。

- [ ] **Step 7: 扩展 Diagnostics 白名单**

先把 `tests/test_diagnostics.py::_DIAGNOSTIC_KEYS` 增加：

```python
{
    "panel_mode",
    "night_light",
    "fan_level",
    "ai_target_temperature",
    "panel_state_available",
}
```

构造 Coordinator 值时断言输出为规范化值；构造恶意/错误属性时断言 mode 为 `unavailable`、
bool/int 为 `None`、availability 为 `False`，且序列化结果不含输入 sentinel。

在 `diagnostics.py` 增加允许集合和 fail-closed helpers：

```python
_PANEL_MODES = frozenset((*PANEL_MODE_OPTIONS, "unavailable"))

def _safe_optional_bool(value: object) -> bool | None:
    return value if type(value) is bool else None

def _safe_optional_int(value: object, *, minimum: int, maximum: int) -> int | None:
    return value if type(value) is int and minimum <= value <= maximum else None
```

输出键固定为：

```python
result["panel_mode"] = _safe_enum(
    _safe_getattr(coordinator, "panel_mode", "unavailable"),
    _PANEL_MODES,
    "unavailable",
)
result["night_light"] = _safe_optional_bool(
    _safe_getattr(coordinator, "night_light_is_on", None)
)
result["fan_level"] = _safe_optional_int(
    _safe_getattr(coordinator, "fan_level", None), minimum=1, maximum=5
)
result["ai_target_temperature"] = _safe_optional_int(
    _safe_getattr(coordinator, "ai_target_temperature", None),
    minimum=30,
    maximum=42,
)
result["panel_state_available"] = _safe_bool(
    _safe_getattr(coordinator, "panel_state_available", False)
)
```

- [ ] **Step 8: 运行实体、Diagnostics 和接线测试**

Run:

```bash
uv run pytest -q \
  tests/test_sensor.py \
  tests/test_binary_sensor.py \
  tests/test_diagnostics.py \
  tests/test_manifest.py \
  tests/test_config_flow.py \
  tests/test_light.py
```

Expected: all pass, including WSS-to-HTTPS removal and listener cleanup.

- [ ] **Step 9: 检查 diff 并提交**

```bash
git diff --check
git add \
  custom_components/aupu_q360/__init__.py \
  custom_components/aupu_q360/binary_sensor.py \
  custom_components/aupu_q360/diagnostics.py \
  custom_components/aupu_q360/entity.py \
  custom_components/aupu_q360/sensor.py \
  custom_components/aupu_q360/strings.json \
  custom_components/aupu_q360/translations/zh-Hans.json \
  tests/test_binary_sensor.py \
  tests/test_diagnostics.py \
  tests/test_manifest.py \
  tests/test_sensor.py
git commit -m "feat(状态映射): 添加只读面板实体"
```

---

### Task 4: 完整删除临时探针与关联网络表面

**Files:**
- Delete: `custom_components/aupu_q360/probe.py`
- Delete: `custom_components/aupu_q360/services.py`
- Delete: `custom_components/aupu_q360/services.yaml`
- Delete: `docs/q360-read-only-discovery-runbook.md`
- Delete: `tests/test_probe.py`
- Delete: `tests/test_probe_network_boundary.py`
- Delete: `tests/test_services.py`
- Modify: `custom_components/aupu_q360/__init__.py:1-159`
- Modify: `custom_components/aupu_q360/models.py:1-161`
- Modify: `custom_components/aupu_q360/coordinator.py:1-327`
- Modify: `custom_components/aupu_q360/shadow.py:22-49`
- Modify: `custom_components/aupu_q360/wss.py:1-180`
- Modify: `custom_components/aupu_q360/strings.json`
- Modify: `custom_components/aupu_q360/translations/zh-Hans.json`
- Modify: `README.md:7-124`
- Modify: `tests/test_manifest.py:30-105`
- Modify: `tests/test_shadow.py:20-63`
- Modify: `tests/test_wss.py:300-525`
- Modify: `tests/ha_runtime/test_ha_runtime.py:29-41,560-870`

**Interfaces:**
- Consumes: Tasks 1–3 已经不依赖 probe 的正式 parser、Coordinator 和实体。
- Produces: 没有 HA domain services、没有 probe runtime、没有 correlated get/renewal；唯一网络读取仍是每次 WSS 建连后的空 `{}` Shadow `get`。

- [ ] **Step 1: 写入真实 HA Action 未注册测试并确认 red**

删除 `test_manifest.py::test_temporary_probe_public_contract_is_exact`，不要用源码文本或文件存在性
断言替代它。把以下行为测试加入 `tests/ha_runtime/test_ha_runtime.py`：

```python
async def test_real_entry_registers_no_temporary_probe_actions(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="AUPU Q360",
        unique_id="synthetic-no-probe-entry",
        data=_entry_data(
            token=_jwt(
                expires_in=7 * 24 * 60 * 60,
                subject="synthetic-no-probe",
            )
        ),
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    for service in ("start_probe", "sample_probe", "stop_probe"):
        assert not hass.services.has_service(DOMAIN, service)
    await _unload(hass, entry)
```

Run:

```bash
AUPU_RUN_HA_RUNTIME=1 uv run --group ha-test pytest \
  tests/ha_runtime/test_ha_runtime.py::test_real_entry_registers_no_temporary_probe_actions \
  -m ha_runtime -v
```

Expected: fails because the current runtime still registers all three probe Actions.

实现删除时，把 `test_english_and_simplified_chinese_flow_keys_match()` 的顶层 key 集合改为
`{"config", "options", "issues", "entity"}`，不再期待 `services` 或 `exceptions`。文件和符号删除
只在 Step 8 的一次性交付门禁中核对，不保留源码 change-detector 单测。

- [ ] **Step 2: 简化 Config Entry runtime 生命周期**

在 `__init__.py` 删除 probe/services imports、observer closures、HA stop listener 和 service
registration。构造 Coordinator 后只保留：

```python
entry.runtime_data.coordinator = coordinator
entry.runtime_data.stoppers.append(coordinator)
try:
    await coordinator.async_start()
    await hass.config_entries.async_forward_entry_setups(entry, _PLATFORMS)
except BaseException:
    await _async_teardown_runtime(entry)
    raise
entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
return True
```

`async_unload_entry()` 只 unload `_PLATFORMS` 后调用 `_async_teardown_runtime()`。

在 `models.py` 删除 TYPE_CHECKING 下的 `PanelStateProbe` import 和 `AupuRuntimeData.probe` 字段。

- [ ] **Step 3: 删除 Coordinator probe ownership**

从 `coordinator.py` 删除：

- `ProbeObserver`、`ProbeCancel`；
- `_probe_observer`、`_probe_cancel`；
- `probe_available`；
- `async_request_shadow_get()`；
- `async_prepare_probe_transport()`；
- `async_set_probe_observer()`；
- stop/disconnect/auth failure 中的 probe cancel 分支；
- `async_apply_shadow_message()` 尾部的 observer 调用。

删除不再使用的 `asyncio` import。保留 `aiohttp` session 类型、`AupuError` 照明错误处理和 Task 2
的面板状态逻辑。

- [ ] **Step 4: 删除 WSS correlated get 与 renewal**

从 `wss.py` 删除 `json`、`re`、`_PROBE_TOKEN`、`_active_websocket`、
`async_request_shadow_get()`、`async_renew_and_wait_healthy()` 和只为 renewal cancellation 存在的
`finish_cleanup_on_cancellation` 分支。`_async_stop_runner()` 恢复为单一普通 stop 路径。

必须保留并由测试精确断言：

```python
await websocket.send_bytes(
    encode_publish(
        f"$aws/things/{self._device.did}/shadow/get",
        b"{}",
    )
)
```

删除 `tests/test_wss.py` 中 renew/correlated-get 专项测试；增强现有真实握手测试，用实际发送的
MQTT packets 证明第 4 个 packet 是唯一 PUBLISH，且 topic 为 `shadow/get`、payload 为 `b"{}"`：

```python
packets = [decode_packets(raw)[0] for raw in websocket.sent[:4]]
assert [packet.packet_type for packet in packets] == [
    PacketType.CONNECT,
    PacketType.SUBSCRIBE,
    PacketType.SUBSCRIBE,
    PacketType.PUBLISH,
]
publishes = [
    packet for packet in packets if packet.packet_type is PacketType.PUBLISH
]
assert len(publishes) == 1
assert publishes[0].topic == (
    "$aws/things/123456789/shadow/get"
)
assert publishes[0].payload == b"{}"
```

- [ ] **Step 5: 删除 AcceptedShadow correlation 字段**

把 dataclass 收窄为：

```python
@dataclass(frozen=True, slots=True)
class AcceptedShadow:
    """One validated target Shadow message with private parsed state."""

    topic_kind: Literal["get", "update"]
    state: dict[str, Any] = field(repr=False)
```

`parse_accepted_shadow()` 不再读取或校验顶层 `clientToken`。更新 `tests/test_shadow.py`：传入一个
合成 `clientToken` 仍应成功解析，但返回对象没有 `client_token` 属性，且 repr 不包含 token、
state 或 topic；删除旧 token 类型/长度测试。

- [ ] **Step 6: 删除 Action、翻译、文件和旧测试**

使用 `apply_patch` 精确删除本任务列出的七个文件。`strings.json` 与 `zh-Hans.json` 删除顶层
`services`、`exceptions`；保留 Task 3 的 `entity`、现有 config/options/issues。

删除 HA runtime 中三段 probe service 测试、`_shadow_state()`、
`_call_probe_snapshot_action()`、service schema test 及其 imports。新增一个 WSS Config Entry 断言：

```python
for service in ("start_probe", "sample_probe", "stop_probe"):
    assert not hass.services.has_service(DOMAIN, service)
```

- [ ] **Step 7: 把 README 改为正式只读面板状态**

支持范围明确写成：现有主照明可控；当前运行模式、小夜灯、风量档位和 AI 目标温度只读；模式、
夜灯、档位和温度不能由本集成控制；剩余时间未映射。

用“只读面板状态”替换整个临时探针章节，并包含以下事实：

- 四个实体只在启用 WSS 时创建；
- 数据只来自目标设备 Shadow `reported`；
- 运行模式是一个互斥 enum；
- 断线或当前连接尚未确认字段时实体 unavailable；
- 未识别模式显示“未知”；
- 不需要 HAR/PCAP，也没有 probe Action 或原始数据存储。

故障排查段将 Diagnostics 描述更新为正式规范化状态白名单，不再提临时探针。

- [ ] **Step 8: 运行删除与回归测试**

Run:

```bash
uv run pytest -q \
  tests/test_manifest.py \
  tests/test_shadow.py \
  tests/test_wss.py \
  tests/test_config_flow.py \
  tests/test_coordinator.py \
  tests/test_sensor.py \
  tests/test_binary_sensor.py \
  tests/test_light.py
```

Expected: all pass; test collection contains no deleted probe/service files.

再运行静态删除检查：

```bash
test ! -e custom_components/aupu_q360/probe.py
test ! -e custom_components/aupu_q360/services.py
test ! -e custom_components/aupu_q360/services.yaml
test ! -e docs/q360-read-only-discovery-runbook.md
test ! -e tests/test_probe.py
test ! -e tests/test_probe_network_boundary.py
test ! -e tests/test_services.py
! rg -n "PanelStateProbe|start_probe|sample_probe|stop_probe|disc-" \
  custom_components/aupu_q360 README.md tests \
  --glob '!tests/fixtures/**'
! rg -n "clientToken|client_token" custom_components/aupu_q360 README.md
```

Expected: all `test !` commands succeed and `rg` prints nothing.

- [ ] **Step 9: 检查 diff 并提交**

```bash
git diff --check
git status --short
git add -A \
  README.md \
  custom_components/aupu_q360 \
  docs/q360-read-only-discovery-runbook.md \
  tests
git commit -m "refactor(状态映射): 删除临时状态探针"
```

提交前用 `git diff --cached --name-status` 确认没有历史 specs/plans、`.codegraph/` 或无关文件。

---

### Task 5: 锁定 0.3.0 public contract 与真实 HA runtime

**Files:**
- Modify: `custom_components/aupu_q360/const.py:1-4`
- Modify: `custom_components/aupu_q360/manifest.json:1-13`
- Modify: `pyproject.toml:1-5`
- Modify: `uv.lock:686-689`
- Modify: `tests/test_manifest.py:12-15`
- Modify: `tests/ha_runtime/test_ha_runtime.py:440-870`

**Interfaces:**
- Consumes: Tasks 1–4 的最终正式 runtime 和实体。
- Produces: 同步版本 `0.3.0`；真实 HA Config Entry manager 对五个 WSS-only 只读实体和现有 light 的端到端证明。

- [ ] **Step 1: 先把版本契约测试改为 0.3.0 并确认 red**

把 `tests/test_manifest.py` 的 `VERSION` 改为：

```python
VERSION = "0.3.0"
```

Run: `uv run pytest -q tests/test_manifest.py::test_manifest_is_hacs_installable`

Expected: fails because manifest、pyproject、const 和 lock still report `0.2.4`.

- [ ] **Step 2: 同步四处版本并更新 lock**

把 `manifest.json`、`pyproject.toml` 和 `const.py` 的版本改成 `0.3.0`，然后运行：

```bash
uv lock --offline
uv lock --check --offline
```

Expected: `uv.lock` root virtual package is `0.3.0`; no dependency resolution changes beyond project version.

- [ ] **Step 3: 用正式状态测试替换 probe HA runtime 测试**

复用现有 `_FakeWebSocket`、`_FakeSession`、`_ControlledSleep` 和 `_queue_shadow_update`，建立 WSS
Config Entry。初始连接后，先断言 registry 有六个总实体：一个 light、两个 binary_sensor、三个
sensor；四个面板实体 state 都是 `STATE_UNAVAILABLE`。

向已订阅 socket 注入以下合成 `get/accepted`：

```python
await _queue_shadow_update(
    hass,
    websocket,
    {
        "reported": {
            "123456789": {
                "2": {"properties": {"1": False}},
                "3": {"properties": {"2": 0, "3": 36}},
                "6": {"properties": {"4": False, "5": 5}},
            }
        }
    },
    topic_suffix="get/accepted",
)
```

如果现有 helper 没有 `topic_suffix`，把它改成 keyword-only 参数，默认
`"update/accepted"`，并用该 suffix 构造目标 topic。

按 entity registry unique ID 后缀定位实体，断言：

```python
assert hass.states.get(mode.entity_id).state == "off"
assert hass.states.get(night_light.entity_id).state == "off"
assert hass.states.get(fan.entity_id).state == "5"
assert hass.states.get(temperature.entity_id).state == "36"
assert hass.states.get(channel.entity_id).state == "on"
assert hass.states.get(light.entity_id).state == "off"
for service in ("start_probe", "sample_probe", "stop_probe"):
    assert not hass.services.has_service(DOMAIN, service)
```

再注入只包含 `3/3=34` 和 `6/4=true` 的 partial update，断言 mode/fan 保持 `off/5`，temperature
与 night light 变成 `34/on`，且 fake `set_light` control calls 仍为空。

- [ ] **Step 4: 扩展 WSS-to-HTTPS runtime 清理测试**

把现有测试的 WSS 初始实体域断言改为：

```python
assert sorted(entity.domain for entity in entities) == [
    "binary_sensor",
    "binary_sensor",
    "light",
    "sensor",
    "sensor",
    "sensor",
]
```

reload 为 `use_wss=false` 后精确断言只剩原 light；五个 WSS-only registry entries 和 states 都
删除，WSS tasks 为空，socket 最后收到 MQTT DISCONNECT。

- [ ] **Step 5: 运行 HA runtime 和 public-contract 测试**

Run:

```bash
uv run pytest -q tests/test_manifest.py tests/test_diagnostics.py
AUPU_RUN_HA_RUNTIME=1 uv run --group ha-test pytest \
  tests/ha_runtime -m ha_runtime -v
```

Expected: both commands pass; runtime test confirms six total entities for WSS and one light for HTTPS-only.

- [ ] **Step 6: 检查版本 diff 并提交**

```bash
git diff --check
git diff -- \
  custom_components/aupu_q360/const.py \
  custom_components/aupu_q360/manifest.json \
  pyproject.toml \
  uv.lock \
  tests/test_manifest.py \
  tests/ha_runtime/test_ha_runtime.py
git add \
  custom_components/aupu_q360/const.py \
  custom_components/aupu_q360/manifest.json \
  pyproject.toml \
  uv.lock \
  tests/test_manifest.py \
  tests/ha_runtime/test_ha_runtime.py
git commit -m "chore(release): 准备 0.3.0 面板状态版本"
```

---

## Scope 1: 独立 worktree 实施与完整验证

- [ ] **Step 1: 创建独立实施 worktree**

执行时先使用 `superpowers:using-git-worktrees`。从包含 spec 与 plan 的 `main` 创建：

```bash
git worktree add \
  /home/george/projects/python/ha-aupu-q360-panel-state \
  -b feature/q360-formal-panel-state \
  main
```

不要删除或修改现有 `/home/george/projects/python/ha-aupu-q360-ablation`。

- [ ] **Step 2: 在每个任务之间核对提交边界**

每个 Task 后运行：

```bash
git status --short --branch
git show --stat --oneline --summary HEAD
```

Expected: working tree clean; exactly one task commit added; no `.codegraph/` or private artifact tracked.

- [ ] **Step 3: 运行最终离线完整门禁**

在实施 worktree 运行：

```bash
uv lock --check --offline
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy custom_components/aupu_q360
uv run python scripts/check_no_secrets.py
uv run python scripts/verify_private_signer.py
git diff --check
```

Expected:

- lock check succeeds；
- pytest has zero failures；
- Ruff lint and format print no errors；
- mypy reports success；
- secret scanner reports no findings；
- private signer reports the existing safe 7/7 verification without printing private material；
- `git diff --check` prints nothing。

- [ ] **Step 4: 运行真实 Home Assistant pytest runtime**

```bash
AUPU_RUN_HA_RUNTIME=1 uv run --group ha-test pytest \
  tests/ha_runtime -m ha_runtime -v
```

Expected: all HA runtime tests pass, no leaked `aupu_q360_wss*` tasks.

- [ ] **Step 5: 审查最终树与网络边界**

```bash
rg -n "service/3/property/2|service/6/property/4|service/6/property/5|service/3/property/3" \
  custom_components/aupu_q360 tests README.md
! rg -n "service/6/property/1|service/4/property/1|service/6/property/23" \
  custom_components/aupu_q360 README.md
! rg -n "PanelStateProbe|start_probe|sample_probe|stop_probe|disc-" \
  custom_components/aupu_q360 README.md tests \
  --glob '!tests/fixtures/**'
! rg -n "clientToken|client_token" custom_components/aupu_q360 README.md
git ls-files | rg '(^|/)(probe\.py|services\.py|services\.yaml|test_probe\.py|test_probe_network_boundary\.py|test_services\.py|q360-read-only-discovery-runbook\.md)$' \
  && exit 1 || true
git status --short --branch
```

Expected: four formal paths appear only in parser/tests/docs; three excluded paths and all probe identifiers print
nothing; deleted filenames are not tracked; worktree is clean.

---

## Scope 2: Fast-forward main 与 push

用户已明确要求本轮完成 commit 与 push；禁止 force push、rebase 或历史改写。

- [ ] **Step 1: 重新核实两个 worktree**

在主 checkout 运行：

```bash
git -C /home/george/projects/python/ha-aupu-q360 status --short --branch
git -C /home/george/projects/python/ha-aupu-q360-panel-state status --short --branch
git worktree list --porcelain
```

Expected: main and feature worktrees clean; old ablation worktree still present and untouched.

- [ ] **Step 2: Fast-forward main**

```bash
git -C /home/george/projects/python/ha-aupu-q360 merge \
  --ff-only feature/q360-formal-panel-state
```

Expected: main advances by exactly the five implementation commits; no merge commit.

- [ ] **Step 3: 在 main 上重新运行高价值验证**

```bash
cd /home/george/projects/python/ha-aupu-q360
uv lock --check --offline
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy custom_components/aupu_q360
uv run python scripts/check_no_secrets.py
AUPU_RUN_HA_RUNTIME=1 uv run --group ha-test pytest \
  tests/ha_runtime -m ha_runtime -v
git diff --check
git status --short --branch
```

Expected: every command passes and main is clean ahead of `origin/main`.

- [ ] **Step 4: 推送并独立确认远端**

```bash
git push origin main
git rev-parse HEAD
git rev-parse origin/main
git ls-remote --heads origin main
```

Expected: local HEAD、remote-tracking ref and `ls-remote` hash all equal; no tag or release is created.

---

## Scope 3: 本机 HA 精确部署、重启与只读验证

用户已明确要求本轮部署并走完流程。不得读取、复用或输出聊天中出现过的 HA token；部署验证只
使用容器、HTTP 无凭据探针、Recorder、日志和运行文件。

- [ ] **Step 1: 部署前只读预检**

```bash
date --iso-8601=seconds
git -C /home/george/projects/python/ha-aupu-q360 status --short --branch
git -C /home/george/projects/python/ha-aupu-q360 rev-parse HEAD origin/main
docker ps --filter name='^/homeassistant$' \
  --format 'name={{.Names}} status={{.Status}} image={{.Image}}'
docker inspect homeassistant --format \
  '{{range .Mounts}}{{if eq .Destination "/config"}}source={{.Source}} destination={{.Destination}} rw={{.RW}}{{end}}{{end}}'
stat -c '%U:%G %a %n' \
  /home/george/docker/homeassistant/config/custom_components/aupu_q360
find /home/george/docker/homeassistant/config/custom_components/aupu_q360 \
  -maxdepth 1 -type f -printf '%f\n' | sort
```

Expected: clean synchronized main; running `homeassistant`; `/config` source is exactly
`/home/george/docker/homeassistant/config`; active component is the exact expected directory.

- [ ] **Step 2: 创建不覆盖历史的部署前备份**

解析一个具体时间戳并确认目标不存在后：

```bash
deploy_stamp=$(date +%Y%m%d-%H%M%S)
backup_name="aupu_q360-before-0.3.0-${deploy_stamp}"
backup_root="/home/george/docker/homeassistant/config/.codex-backups/${backup_name}"
backup_container="/config/.codex-backups/${backup_name}"
test ! -e "$backup_root"
docker exec homeassistant mkdir -m 0755 "$backup_container"
docker exec homeassistant cp -a \
  /config/custom_components/aupu_q360 \
  "$backup_container/aupu_q360"
diff -qr \
  /home/george/docker/homeassistant/config/custom_components/aupu_q360 \
  "$backup_root/aupu_q360"
```

不得删除或覆盖 `.codex-backups/aupu_q360-before-5b638fc` 及其他历史备份。

- [ ] **Step 3: 精确替换活动组件，避免旧 probe 文件残留**

先把活动目录移动到本次备份下的 `active-original`，然后通过 `docker cp` 创建全新 root-owned
运行目录：

```bash
active_component=/home/george/docker/homeassistant/config/custom_components/aupu_q360
test -d "$active_component"
test ! -e "$backup_root/active-original"
docker exec homeassistant mv \
  /config/custom_components/aupu_q360 \
  "$backup_container/active-original"
docker exec homeassistant mkdir /config/custom_components/aupu_q360
docker cp \
  /home/george/projects/python/ha-aupu-q360/custom_components/aupu_q360/. \
  homeassistant:/config/custom_components/aupu_q360/
```

若 `mkdir` 或 `docker cp` 失败，不删除任何目录；在目标不存在时把不完整目录移动为
`$backup_container/failed-copy`，再把 `active-original` 恢复到活动路径，不得重启 HA：

```bash
if [ -e "$active_component" ] && [ ! -e "$backup_root/failed-copy" ]; then
  docker exec homeassistant mv \
    /config/custom_components/aupu_q360 \
    "$backup_container/failed-copy"
fi
if [ ! -e "$active_component" ] && [ -d "$backup_root/active-original" ]; then
  docker exec homeassistant mv \
    "$backup_container/active-original" \
    /config/custom_components/aupu_q360
fi
```

- [ ] **Step 4: 核对运行目录权限、字节与删除项**

```bash
find "$active_component" -type d ! -perm 0755 -print
find "$active_component" -type f ! -perm 0644 -print
find "$active_component" \( ! -user root -o ! -group root \) -print
diff -qr \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  /home/george/projects/python/ha-aupu-q360/custom_components/aupu_q360 \
  "$active_component"
test ! -e "$active_component/probe.py"
test ! -e "$active_component/services.py"
test ! -e "$active_component/services.yaml"
```

Expected: three `find` commands print nothing; diff prints nothing; all probe files are absent.

- [ ] **Step 5: check_config；失败则回滚且不重启**

```bash
docker exec homeassistant python -m homeassistant \
  --script check_config --config /config
```

Expected: exit 0. On failure, execute the following recoverable moves, rerun `check_config`, and report the fixed
error summary without restarting the failed version：

```bash
test ! -e "$backup_root/failed-0.3.0"
docker exec homeassistant mv \
  /config/custom_components/aupu_q360 \
  "$backup_container/failed-0.3.0"
docker exec homeassistant mv \
  "$backup_container/active-original" \
  /config/custom_components/aupu_q360
docker exec homeassistant python -m homeassistant \
  --script check_config --config /config
```

- [ ] **Step 6: 重启并验证基础健康**

记录 restart 前时间，然后：

```bash
restart_started=$(date --iso-8601=seconds)
docker restart homeassistant
docker ps --filter name='^/homeassistant$' \
  --format 'name={{.Names}} status={{.Status}}'
for attempt in $(seq 1 12); do
  if curl --fail --silent --show-error --output /dev/null \
    http://127.0.0.1:8123/; then
    break
  fi
  docker ps --filter name='^/homeassistant$' \
    --format 'name={{.Names}} status={{.Status}}'
  sleep 5
done
curl --fail --silent --show-error --output /dev/null \
  http://127.0.0.1:8123/
```

如 `docker restart` 尚未返回健康 HTTP，使用不超过 60 秒的分段轮询，并每轮查看容器状态；不得
通过重复 restart 掩盖加载失败。

- [ ] **Step 7: 用 Recorder 核实正式实体当前值**

使用只读数据库连接，只输出 entity ID、state 和本地时间：

```bash
sqlite3 -readonly \
  /home/george/docker/homeassistant/config/home-assistant_v2.db \
  "SELECT sm.entity_id, s.state, datetime(s.last_updated_ts, 'unixepoch', 'localtime')
   FROM states s
   JOIN states_meta sm ON sm.metadata_id=s.metadata_id
   WHERE sm.entity_id LIKE '%aupu_q360t5_pro%'
     AND s.state_id=(
       SELECT MAX(s2.state_id) FROM states s2 WHERE s2.metadata_id=s.metadata_id
     )
   ORDER BY sm.entity_id;"
```

重启后的最新行必须证明：

- connectivity 为 `on`；
- current mode 为 `off`；
- night light 为 `off`；
- fan level 为 `5`；
- AI target temperature 为 `36`；
- 原 light 为 `off`。

如果 entity ID 因 HA 名称规范化不同，按本集成 Config Entry 对应 registry unique ID 后缀定位，
但不得读取或输出 auth/token storage。

- [ ] **Step 8: 核实日志、目录和进程收尾**

```bash
docker logs --since "$restart_started" homeassistant 2>&1 \
  | awk 'BEGIN{IGNORECASE=1} /aupu|q360/ && /error|exception|traceback|failed/ {print}'
find /home/george/docker/homeassistant/config/.storage \
  -maxdepth 1 -type f -iname '*aupu*' -printf '%f\n'
ps -eo pid=,args= | awk '/[h]a_probe_client\.py/ {print}'
diff -qr \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  /home/george/projects/python/ha-aupu-q360/custom_components/aupu_q360 \
  "$active_component"
```

Expected: AUPU error filter、Store search、client process search and diff all print nothing. Bluetooth capability
tracebacks without AUPU/Q360 context remain out of scope.

源码和真实 HA runtime 已经从干净进程启动且没有 service registration code，因此三个 probe Action
不会存在；如用户在 HA 开发者工具中查看 Actions，可再做 UI 只读交叉确认，但不得为此读取
`.storage/auth` 或复用已暴露 token。

- [ ] **Step 9: 精确清理本次临时客户端和空探针目录**

先检查目标：

```bash
test -f /tmp/aupu-q360-deploy.swZ2QlDH/ha_probe_client.py
find /home/george/.local/state/ha-aupu-q360/raw-discovery \
  -mindepth 1 -maxdepth 1 -print
```

只有第二条没有输出时执行：

```bash
unlink /tmp/aupu-q360-deploy.swZ2QlDH/ha_probe_client.py
rmdir /home/george/.local/state/ha-aupu-q360/raw-discovery
```

保留 `/tmp/aupu-q360-deploy.swZ2QlDH/aupu_q360-before`、本次 `$backup_root` 和所有其他历史备份；
不清理父目录，不运行递归删除。

- [ ] **Step 10: 最终证据快照**

```bash
git -C /home/george/projects/python/ha-aupu-q360 status --short --branch
git -C /home/george/projects/python/ha-aupu-q360 rev-parse HEAD origin/main
docker ps --filter name='^/homeassistant$' \
  --format 'name={{.Names}} status={{.Status}} image={{.Image}}'
docker inspect homeassistant --format \
  '{{range .Mounts}}{{if eq .Destination "/config"}}source={{.Source}} destination={{.Destination}} rw={{.RW}}{{end}}{{end}}'
```

Expected: main clean and synchronized with origin; HA running; `/config` mount unchanged. Final report includes
test counts、commit hashes、push hash、backup path、Recorder states and any residual risk, but no credentials or
raw Shadow data.

## Rollback Procedure

若重启后的组件 setup、WSS、实体注册或解析失败：

1. 确认 `$backup_root/active-original` 存在且是本次移动的旧组件。
2. 通过 `docker exec homeassistant mv` 把失败的新活动目录移到
   `$backup_container/failed-0.3.0`，并先确认该目标不存在。
3. 通过 `docker exec homeassistant mv` 把 `$backup_container/active-original` 移回
   `/config/custom_components/aupu_q360`。
4. 运行 HA `check_config`；通过后只重启一次 `homeassistant`。
5. 用 Recorder 和日志确认原 light、connectivity 与 AUPU setup 恢复。
6. 不回退 Git、不 force push、不控制真实设备；保留失败目录供本地分析。
