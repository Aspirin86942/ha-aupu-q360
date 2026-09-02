# Q360 状态优先观测实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 为 Q360 灯实体增加可信来源、过期标记和最后确认时间，并在 WSS 模式下提供持续连接状态二进制传感器。

**架构：** `AupuCoordinator` 继续作为唯一状态权威，保存来源、stale 和 HA 接收确认的 UTC 时间；灯实体与新 binary sensor 只投影这些值。沿用现有持续 WSS、心跳、退避重连和重连后 Shadow `get`，不增加轮询或新的云端请求类型。

**技术栈：** Python 3.13、Home Assistant `LightEntity`/`BinarySensorEntity`、AWS IoT Shadow WSS、pytest、pytest-homeassistant-custom-component、Ruff、mypy。

---

## 依据与全局约束

- 设计规格：`docs/superpowers/specs/2026-09-02-q360-state-observability-design.md`。
- 当前实现基线：提交 `6f350e3`，分支 `feat/aupu-q360-ha`。
- 仅 `reported`、`get_reported` 能清除 stale 并更新时间。
- `desired`、`command`、未知状态和断线必须保持 stale。
- 断线保留最后灯值与最后确认时间；灯实体不变成 unavailable。
- `last_confirmed_at` 是 HA 接收时间，只在内存存在，使用 UTC ISO 8601 输出。
- WSS 模式新增一个 connectivity binary sensor；HTTPS-only 模式仍只有灯。
- 不轮询、不真实连接云端、不发送短信、不控制设备、不读取或输出私密证据。
- 本计划只允许本地提交；push、Release 和实机测试需要单独授权。

## 文件结构与职责

- 修改 `custom_components/aupu_q360/coordinator.py`：保存可信来源、stale、最后确认时间和可注入 UTC clock。
- 修改 `custom_components/aupu_q360/light.py`：投影状态来源、stale、确认时间及 WSS 健康属性。
- 创建 `custom_components/aupu_q360/binary_sensor.py`：投影持续 WSS 连接状态。
- 修改 `custom_components/aupu_q360/__init__.py`：同时转发/卸载 light 与 binary_sensor 平台。
- 修改 `custom_components/aupu_q360/strings.json`、`translations/zh-Hans.json`：定义状态通道实体名称。
- 修改 `tests/test_light.py`：锁定可信状态机、灯属性和双平台生命周期。
- 创建 `tests/test_binary_sensor.py`：锁定 WSS/HTTPS-only 实体注册和监听器清理。
- 修改 `tests/test_config_flow.py`：更新平台转发/卸载契约。
- 修改 `tests/test_manifest.py`：锁定双语 entity key 一致性。
- 修改 `tests/ha_runtime/test_ha_runtime.py`：用真实 HA manager 验证两个实体和卸载。
- 修改 `README.md`：说明状态来源、stale、最后确认时间和连续连接语义。

## 任务 1：实现可信状态模型和灯实体投影

**文件：**

- 修改：`custom_components/aupu_q360/coordinator.py:21-190`
- 修改：`custom_components/aupu_q360/light.py:64-79`
- 测试：`tests/test_light.py:251-314`

- [ ] **步骤 1：编写可信状态红灯测试**

在 `tests/test_light.py` 从协调器模块导入 `StateClock`，定义返回 `datetime.now(UTC)` 的 `_test_utc_now()`，再扩展现有 `_coordinator()` 测试辅助函数，使其接收 `now: StateClock = _test_utc_now` 并传给 `AupuCoordinator`。用固定 UTC 时间覆盖初始、确认、推定、断线和重连：

```python
confirmed_at = datetime(2026, 9, 2, 1, 2, 3, tzinfo=UTC)
coordinator = _coordinator(FakeApi(), now=lambda: confirmed_at)

assert coordinator.is_on is None
assert coordinator.state_stale is True
assert coordinator.last_confirmed_at is None
assert coordinator.light_state_source == "unknown"

coordinator.async_apply_wss_connection(connected=True, healthy=True)
coordinator.async_apply_shadow_update(
    LightShadowUpdate(is_on=True, confirmed=True, source="reported")
)

assert coordinator.state_stale is False
assert coordinator.last_confirmed_at == confirmed_at
assert coordinator.light_state_source == "reported"

coordinator.async_apply_light_state(is_on=False, confirmed=False, source="command")
assert coordinator.state_stale is True
assert coordinator.last_confirmed_at == confirmed_at

coordinator.async_apply_wss_connection(connected=False, healthy=False)
assert coordinator.is_on is False
assert coordinator.state_stale is True
assert coordinator.last_confirmed_at == confirmed_at

coordinator.async_apply_wss_connection(connected=True, healthy=False)
assert coordinator.state_stale is True
coordinator.async_apply_shadow_update(
    LightShadowUpdate(is_on=True, confirmed=True, source="get_reported")
)
assert coordinator.state_stale is False
assert coordinator.light_state_source == "get_reported"
```

把确认来源参数化为 `reported/get_reported`，把推定来源参数化为 `desired/command`，锁定只有前者会更新 `last_confirmed_at`。另断言 `async_stop()` 保留最后灯值和确认时间，但设置 `state_stale=True`。

再断言灯实体属性精确为：

```python
assert entity.extra_state_attributes == {
    "state_source": "command",
    "state_stale": True,
    "last_confirmed_at": "2026-09-02T01:02:03+00:00",
    "wss_connected": False,
    "wss_healthy": False,
}
```

- [ ] **步骤 2：运行测试确认红灯**

运行：

```powershell
uv run pytest tests/test_light.py -k "shadow_source or confirmed or stale" -v
```

预期：FAIL，协调器缺少 `state_stale`、`last_confirmed_at` 或 `now` 参数，灯属性也缺少三个新字段。

- [ ] **步骤 3：实现协调器状态字段**

在 `coordinator.py` 增加固定类型和默认时钟：

```python
from datetime import UTC, datetime

StateClock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)
```

构造函数增加 `now: StateClock = _utc_now`，并初始化：

```python
self._now = now
self._state_stale = True
self._last_confirmed_at: datetime | None = None
```

增加只读属性：

```python
@property
def state_stale(self) -> bool:
    return self._state_stale

@property
def last_confirmed_at(self) -> datetime | None:
    return self._last_confirmed_at
```

在 `async_apply_light_state()` 中仅对确认状态更新时间：

```python
self._state_stale = not confirmed
if confirmed:
    confirmed_at = self._now()
    if confirmed_at.tzinfo is None or confirmed_at.utcoffset() is None:
        raise ValueError("State clock must return an aware datetime")
    self._last_confirmed_at = confirmed_at.astimezone(UTC)
```

在 `async_apply_wss_connection()` 中仅当 `connected` 为 false 时设置 `self._state_stale = True`；重连本身不能清除 stale，必须等待新的 `reported/get_reported`。在 `async_stop()` 的 `finally` 中也先设置 `self._state_stale = True` 再清理 listener，覆盖 WSS runner 未启动或已退出的停止路径。

- [ ] **步骤 4：实现灯实体属性投影**

把 `extra_state_attributes` 返回类型改为 `dict[str, object]`，并从协调器投影：

```python
confirmed_at = self._coordinator.last_confirmed_at
return {
    "state_source": self._coordinator.light_state_source,
    "state_stale": self._coordinator.state_stale,
    "last_confirmed_at": confirmed_at.isoformat() if confirmed_at is not None else None,
    "wss_connected": self._coordinator.wss_connected,
    "wss_healthy": self._coordinator.wss_healthy,
}
```

- [ ] **步骤 5：运行任务测试和静态检查**

运行：

```powershell
uv run pytest tests/test_light.py -v
uv run ruff check custom_components/aupu_q360/coordinator.py custom_components/aupu_q360/light.py tests/test_light.py
uv run mypy custom_components/aupu_q360/coordinator.py custom_components/aupu_q360/light.py
```

预期：全部 PASS；`reported/get_reported` 更新时间，`desired/command` 与断线保留时间但标记 stale。

- [ ] **步骤 6：提交任务 1**

```powershell
git add custom_components/aupu_q360/coordinator.py custom_components/aupu_q360/light.py tests/test_light.py
git diff --cached --check
git commit -m "feat: 记录 Q360 状态可信度"
```

## 任务 2：添加持续状态通道二进制传感器

**文件：**

- 创建：`custom_components/aupu_q360/binary_sensor.py`
- 修改：`custom_components/aupu_q360/__init__.py:18`
- 修改：`custom_components/aupu_q360/strings.json`
- 修改：`custom_components/aupu_q360/translations/zh-Hans.json`
- 创建：`tests/test_binary_sensor.py`
- 修改：`tests/test_light.py:416-558`
- 修改：`tests/test_config_flow.py:1054,1080,1441`
- 修改：`tests/test_manifest.py:77-108`

- [ ] **步骤 1：编写平台和实体红灯测试**

创建 `tests/test_binary_sensor.py`，沿用 `tests/test_light.py` 的合成协调器构造方式，并用只含 `entry_id`、`unique_id`、`runtime_data` 的 fake entry 覆盖 WSS 模式实体、HTTPS-only 空集合和监听器：

```python
wss_entities: list[AupuStateChannelBinarySensor] = []
https_entities: list[AupuStateChannelBinarySensor] = []

def add_wss_entities(new_entities: list[AupuStateChannelBinarySensor]) -> None:
    wss_entities.extend(new_entities)

def add_https_entities(new_entities: list[AupuStateChannelBinarySensor]) -> None:
    https_entities.extend(new_entities)

wss_entry = SimpleNamespace(
    entry_id="synthetic-entry",
    unique_id="synthetic-unique-id",
    runtime_data=SimpleNamespace(use_wss=True, coordinator=coordinator),
)
https_only_entry = SimpleNamespace(
    entry_id="synthetic-https-entry",
    unique_id="synthetic-https-unique-id",
    runtime_data=SimpleNamespace(use_wss=False, coordinator=coordinator),
)
light_entity = AupuLight(
    coordinator=coordinator,
    entry_id=wss_entry.entry_id,
    unique_id=wss_entry.unique_id,
)

await async_setup_binary_sensor(hass, wss_entry, add_wss_entities)
assert len(wss_entities) == 1
entity = wss_entities[0]
assert entity.unique_id == "synthetic-unique-id_state_channel"
assert entity.device_class is BinarySensorDeviceClass.CONNECTIVITY
assert entity.is_on is False
assert entity.device_info == light_entity.device_info
assert "123456789" not in entity.unique_id
assert "synthetic-tag" not in repr(entity.device_info)

coordinator.async_apply_wss_connection(connected=True, healthy=False)
assert entity.is_on is True
assert entity.extra_state_attributes == {
    "healthy": False,
    "state_stale": True,
    "last_confirmed_at": None,
}

coordinator.async_apply_wss_connection(connected=False, healthy=False)
assert entity.is_on is False
assert entity.extra_state_attributes["state_stale"] is True

await async_setup_binary_sensor(hass, https_only_entry, add_https_entities)
assert https_entities == []
```

更新 setup/unload 契约断言：

```python
assert config_entries.forwarded == (Platform.LIGHT, Platform.BINARY_SENSOR)
assert config_entries.unloaded == (Platform.LIGHT, Platform.BINARY_SENSOR)
```

更新 `test_manifest.py`，要求英文和简中顶层键包含 `entity`，并精确锁定：

```python
assert strings["entity"]["binary_sensor"]["state_channel"]["name"] == "State channel"
assert translation["entity"]["binary_sensor"]["state_channel"]["name"] == "状态通道"
```

- [ ] **步骤 2：运行测试确认红灯**

运行：

```powershell
uv run pytest tests/test_binary_sensor.py tests/test_light.py tests/test_config_flow.py tests/test_manifest.py -q
```

预期：FAIL，`binary_sensor.py` 不存在，平台元组仍只有 LIGHT，双语文件没有 entity key。

- [ ] **步骤 3：实现 binary sensor**

创建 `binary_sensor.py`，使用与灯相同的设备注册信息和 listener 生命周期：

```python
async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry[AupuRuntimeData],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    del hass
    if not entry.runtime_data.use_wss:
        return
    async_add_entities(
        [
            AupuStateChannelBinarySensor(
                coordinator=entry.runtime_data.coordinator,
                entry_id=entry.entry_id,
                unique_id=f"{entry.unique_id or entry.entry_id}_state_channel",
            )
        ]
    )


class AupuStateChannelBinarySensor(BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "state_channel"

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

    @property
    def is_on(self) -> bool:
        return self._coordinator.wss_connected

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        confirmed_at = self._coordinator.last_confirmed_at
        return {
            "healthy": self._coordinator.wss_healthy,
            "state_stale": self._coordinator.state_stale,
            "last_confirmed_at": confirmed_at.isoformat() if confirmed_at is not None else None,
        }

    async def async_added_to_hass(self) -> None:
        self._remove_listener = self._coordinator.async_add_listener(
            self.async_write_ha_state
        )

    async def async_will_remove_from_hass(self) -> None:
        if self._remove_listener is not None:
            self._remove_listener()
            self._remove_listener = None
```

`async_setup_entry()` 在 `entry.runtime_data.use_wss` 为 false 时不添加实体；启用时 unique ID 使用 `(entry.unique_id or entry.entry_id) + "_state_channel"`，DeviceInfo 与灯实体完全一致。实体在 `async_added_to_hass()` 注册协调器 listener，在 `async_will_remove_from_hass()` 精确移除一次。测试先记录移除前的通知次数，连续调用两次 `async_will_remove_from_hass()`，并证明后续协调器更新不会再写实体状态。

- [ ] **步骤 4：注册双平台并补双语实体名称**

把 `__init__.py` 改为：

```python
_PLATFORMS = (Platform.LIGHT, Platform.BINARY_SENSOR)
```

英文和简中 JSON 都增加同构键：

```json
"entity": {
  "binary_sensor": {
    "state_channel": {
      "name": "State channel"
    }
  }
}
```

简中 `name` 使用 `状态通道`。更新测试 fake platform loader，按平台分派并保存两种实体：

```python
for platform in platforms:
    if platform is Platform.LIGHT:
        await async_setup_light(hass, entry, add_entities)
    elif platform is Platform.BINARY_SENSOR:
        await async_setup_binary_sensor(hass, entry, add_entities)
for entity in self.entities:
    await entity.async_added_to_hass()
```

对应的卸载 fake 对每个已注册实体调用 `async_will_remove_from_hass()`，并断言 listener 数量回到零。

- [ ] **步骤 5：运行任务测试和静态检查**

运行：

```powershell
uv run pytest tests/test_binary_sensor.py tests/test_light.py tests/test_config_flow.py tests/test_manifest.py -q
uv run ruff check custom_components/aupu_q360/binary_sensor.py custom_components/aupu_q360/__init__.py tests/test_binary_sensor.py
uv run mypy custom_components/aupu_q360
```

预期：全部 PASS；WSS 配置产生两个同设备实体，HTTPS-only 仍只有灯，卸载后没有 listener。

- [ ] **步骤 6：提交任务 2**

```powershell
git add custom_components/aupu_q360/__init__.py custom_components/aupu_q360/binary_sensor.py custom_components/aupu_q360/strings.json custom_components/aupu_q360/translations/zh-Hans.json tests/test_binary_sensor.py tests/test_light.py tests/test_config_flow.py tests/test_manifest.py
git diff --cached --check
git commit -m "feat: 添加 Q360 状态通道传感器"
```

## 任务 3：完成真实 HA runtime、文档和全量验收

**文件：**

- 修改：`tests/ha_runtime/test_ha_runtime.py:234-370`
- 修改：`README.md:61-68`
- 测试：全部 `tests/`

- [ ] **步骤 1：扩展 Linux HA runtime 测试**

保持 HTTPS-only 用例只断言一个 light；在现有 fake WSS 生命周期用例中读取真实 entity registry：

```python
entities = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
assert sorted(entity.domain for entity in entities) == ["binary_sensor", "light"]
channel = next(entity for entity in entities if entity.domain == "binary_sensor")
channel_state = hass.states.get(channel.entity_id)
assert channel_state is not None
assert channel_state.state == "on"
assert channel_state.attributes["healthy"] is False
assert channel_state.attributes["state_stale"] is True
```

通过 `entry.runtime_data.coordinator.async_apply_shadow_update(...)` 注入一个 `reported` 更新，调用 `await hass.async_block_till_done()`，再断言 light 的 `state_source=reported`、`state_stale=false`、`last_confirmed_at` 是非空 UTC ISO 8601 值。调用 `async_apply_wss_connection(False, False)`，证明灯保留原值但变 stale，状态通道实体变为 `off`；卸载后两个实体都从状态机移除且现有 WSS task leak 断言继续通过。

- [ ] **步骤 2：运行本地可执行红灯/绿灯门**

Windows 不加载 HA pytest plugin，但必须先保证 runtime 源码可编译：

```powershell
uv run python -m py_compile tests/ha_runtime/test_ha_runtime.py
uv run ruff check tests/ha_runtime/test_ha_runtime.py
```

预期：PASS。真实 manager 运行只在 Linux `ha-runtime` job 或等价 Linux 环境执行，不能在 Windows 报告为已通过。

- [ ] **步骤 3：更新 README 状态语义**

把“状态与控制语义”扩展为以下明确内容：

- 持续 WSS 不做周期轮询，首次连接和每次重连后各执行一次 Shadow `get`。
- `reported/get_reported` 是设备确认；`desired/command` 是推定。
- 断线保留最后值但 `state_stale=true`。
- `last_confirmed_at` 是 HA 接收确认的 UTC 时间，重启后由首次 Shadow `get` 重建。
- WSS 模式提供状态通道 connectivity binary sensor；HTTPS-only 模式不创建。

不得声称浴霸面板变化已经实机验证。

- [ ] **步骤 4：运行完整离线验收矩阵**

```powershell
uv sync --locked
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy custom_components/aupu_q360
uv run python scripts/verify_private_signer.py
uv run python scripts/check_no_secrets.py
git diff --check
git status --short
```

预期：全部 PASS；私有签名安全计数 7/7；秘密扫描 0 命中；不输出私密路径和值。

- [ ] **步骤 5：在获授权的 Linux/GitHub 环境验证 HA runtime**

运行：

```bash
AUPU_RUN_HA_RUNTIME=1 uv run --group ha-test pytest tests/ha_runtime -m ha_runtime -v
```

预期：PASS，真实 HA manager 注册两个 WSS 模式实体、HTTPS-only 一个实体，卸载后无状态或任务残留。没有用户对 push/CI 的单独授权时，只记录该门待执行，不推送。

- [ ] **步骤 6：提交任务 3**

```powershell
git add README.md tests/ha_runtime/test_ha_runtime.py
git diff --cached --check
git commit -m "docs: 完成 Q360 状态观测验收"
```

## 最终交付边界

- 最终报告必须区分本地合成测试、Linux HA runtime 和真实设备验证。
- 不得把 WSS 连续运行测试表述为浴霸面板变化已实机上报。
- 不自动 push、创建 Release、安装 HACS 或控制真实设备。
- 若用户之后授权 push，推送前重新运行秘密扫描，并验证本地 HEAD、远程跟踪引用和 GitHub 服务端 SHA 一致。
