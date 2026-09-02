# Q360 只读状态发现实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不增加设备控制能力的前提下，复用现有唯一 AWS IoT WSS 通道完成 Q360 `state.reported` 的一次性、可审计、可脱敏只读字段发现。

**Architecture:** 传输层先把已通过 Topic、MQTT 大小和 JSON 边界校验的 accepted Shadow 解码为短生命周期消息，再依次交给原照明解析器和可选发现会话；发现异常被固定文本隔离，不能影响照明状态或重连。每个 Config Entry 只有一个内存状态机，使用带 `clientToken` 的 Shadow `get` 建立基线和步骤快照，立即把目标设备路径和值转成会话内脱敏表示，最终只通过 Home Assistant `Store` 原子保存最近一份已二次扫描的报告，并把它并入集成诊断。

**Tech Stack:** Python 3.13.2+、Home Assistant 2026.x Config Entry/Action/Diagnostics/Store API、AWS IoT Device Shadow MQTT over WSS、`asyncio`、pytest、pytest-homeassistant-custom-component、Ruff、mypy、uv。

**Spec:** `docs/superpowers/specs/2026-09-02-q360-read-only-state-discovery-design.md`

## Global Constraints

- 当前实现基线是 `main` 的 `e821d55`；实施前重新核验分支、HEAD 和工作区，不假设该快照仍未变化。
- 只读取 `state.reported.<device-id>.<service-id>.properties.<property-id>`；`desired` 不参与确认评分。
- 只允许现有照明 HTTPS 控制和 Shadow `get`；发现代码不得新增其他 MQTT publish、控制 API 或第二条 WSS。
- accepted MQTT 包继续受 `64 * 1024` 字节上限保护；快照响应超时 10 秒、单步骤超时 2 分钟、会话超时 20 分钟、每步最多 256 条已脱敏变化。
- 能力枚举固定为 `heating`、`ventilation`、`drying`、`swing`、`fan_level`、`timer`、`idle_environment`。
- 目标枚举固定为 `off`、`on`、`level_1`、`level_2`、`level_3`；三个 `level_*` 只是受控实验标签，不声称设备实际提供三个档位。
- 能力与目标矩阵固定为：普通功能 `off/on`，`fan_level` 为 `off/level_1/level_2/level_3`，`idle_environment` 只允许 `off`。
- 字符串原文、原始时间戳、设备 ID、Tag、Config Entry ID、实体唯一 ID、凭据、完整 Topic、client token 和原始 Shadow 不得进入报告、异常或日志。
- 原始 Shadow 仅存在于当前调用栈和当前快照等待对象；步骤记录只保存规范化路径和脱敏值。完成、取消、断线、鉴权失败、卸载和 HA 停止都必须清理会话内存与监听器。
- `confirmed_candidate` 只表示字段与受控实验重复相关，不能自动创建实体或证明温度、湿度、功率语义；正式映射另行设计。
- checkout 保持在 `/home/george/projects/python/ha-aupu-q360`，HA `/config` 当前只读核验为 `/home/george/docker/homeassistant/config` 的容器挂载；开发和 HA 运行目录不得合并。
- 当前主机已核验 `uv 0.12.9`，但只发现系统 Python 3.11.2，`uv python find 3.13` 尚找不到 Python 3.13。执行代码任务前必须先由用户单独授权并完成 Python 3.13.2+ 准备；本计划不自动下载解释器、安装全局工具或修改系统包。
- 当前主机已核验 `codegraph 1.6.0`，但仓库根目录没有 `.codegraph/`；按项目边界不自行创建索引，只有用户建立索引后才在后续调用链任务中优先使用 CodeGraph。
- 不读取仓库根目录的 `config_entry-*.json`，不恢复或迁移 PCAP/HAR/Cookie/证书/`.private/`；自动化测试只能使用合成标识和值。
- 本地修改、提交、同步到 HA、重启 HA、真实发现会话、push、Release 和正式实体映射是相互独立的授权阶段。计划中的提交命令只有在用户明确授权本地提交后才能执行。

---

## 文件结构与职责

- 修改 `custom_components/aupu_q360/shadow.py`：一次解码 accepted Shadow，提供短生命周期 `AcceptedShadow`，并保持照明路径语义不变。
- 修改 `custom_components/aupu_q360/wss.py`：保持唯一连接，增加受控、相关联的只读 Shadow `get` 发送接口。
- 修改 `custom_components/aupu_q360/coordinator.py`：继续作为照明权威，并提供窄 `get` 接口和隔离的发现观察器生命周期。
- 创建 `custom_components/aupu_q360/discovery_models.py`：定义状态、受控标签、脱敏值、步骤证据、候选和报告 JSON 类型。
- 创建 `custom_components/aupu_q360/discovery_sanitizer.py`：目标路径提取、值脱敏、资源限制和最终报告禁止字段扫描。
- 创建 `custom_components/aupu_q360/discovery_analysis.py`：快照差分、瞬时变化归并和候选分类。
- 创建 `custom_components/aupu_q360/discovery.py`：20 分钟会话、步骤状态机、相关快照等待、超时和清理。
- 创建 `custom_components/aupu_q360/discovery_store.py`：Home Assistant 管理的私有、原子、最近一份报告存储。
- 创建 `custom_components/aupu_q360/services.py`：五个 Config Entry 定向 Action 的 schema、注册、卸载和固定响应。
- 创建 `custom_components/aupu_q360/services.yaml`：Action UI 描述和受控 selector。
- 修改 `custom_components/aupu_q360/models.py`、`__init__.py`：组装每 Entry 运行时，管理服务、HA stop、卸载和 Entry 删除。
- 修改 `custom_components/aupu_q360/diagnostics.py`：在原有白名单诊断中加入已验证的最近报告。
- 修改 `custom_components/aupu_q360/strings.json`、`translations/zh-Hans.json`：Action、响应和固定错误码双语文本。
- 创建 `tests/test_discovery_sanitizer.py`、`tests/test_discovery_analysis.py`、`tests/test_discovery.py`、`tests/test_services.py`：发现功能的纯合成单元测试。
- 修改 `tests/test_shadow.py`、`test_wss.py`、`test_diagnostics.py`、`test_config_flow.py`、`test_manifest.py`：锁定现有边界和新增生命周期。
- 修改 `tests/ha_runtime/test_ha_runtime.py`：在真实 HA 事件循环、服务注册表、Store 和 Config Entry manager 上验证。
- 创建 `docs/q360-read-only-discovery-runbook.md`，修改 `README.md`：记录本地验证、授权部署、回滚和实体面板实验步骤。

## Task 1：建立 accepted Shadow 消息与相关只读 get

**文件：**

- 修改：`custom_components/aupu_q360/shadow.py:1-98`
- 修改：`custom_components/aupu_q360/wss.py:29-274`
- 修改：`custom_components/aupu_q360/coordinator.py:31-227`
- 测试：`tests/test_shadow.py`
- 测试：`tests/test_wss.py`
- 测试：`tests/test_light.py`

**接口：**

- 产生：`AcceptedShadow(topic_kind: Literal["get", "update"], state: dict[str, Any], client_token: str | None)`。
- 产生：`parse_accepted_shadow(device, topic, payload) -> AcceptedShadow | None`。
- 产生：`parse_light_shadow_update(device, message) -> LightShadowUpdate | None`，并保留兼容包装 `parse_shadow_update(device, topic, payload)`。
- 产生：`AupuShadowWebSocket.async_request_shadow_get(client_token: str) -> None`。
- 产生：`AupuCoordinator.async_request_shadow_get(client_token: str) -> None`、`discovery_available: bool` 与 `async_set_discovery_observer(observer, cancel)`。

- [ ] **步骤 1：执行不改变状态的工具链和仓库门**

运行：

```bash
pwd
git rev-parse --show-toplevel
git branch --show-current
git status --short
command -v uv
python3 --version
```

预期：仓库根目录是 `/home/george/projects/python/ha-aupu-q360`、分支为 `main`、开始实施前工作区状态已由执行者确认；`uv` 必须可用且 `uv run python --version` 不低于 3.13.2。若仍与计划编写时一样缺少 Python 3.13，停止并报告 `development_toolchain_missing`，等待用户单独授权准备解释器，不执行下载或安装命令。

- [ ] **步骤 2：编写 accepted Shadow 与双消费者红灯测试**

在 `tests/test_shadow.py` 增加下列核心断言：

```python
message = parse_accepted_shadow(
    DEVICE,
    GET_ACCEPTED,
    b'{"clientToken":"disc-0123456789abcdef0123456789abcdef",'
    b'"state":{"reported":{"123":{"2":{"properties":{"1":true,"9":22.5}}}}}}',
)
assert message is not None
assert message.topic_kind == "get"
assert message.client_token == "disc-0123456789abcdef0123456789abcdef"
assert parse_light_shadow_update(DEVICE, message) == LightShadowUpdate(
    True, True, "get_reported"
)
```

继续参数化错误输入：非 accepted Topic 返回 `None`；非 bytes、非法 UTF-8、非标准 JSON 常量、非对象文档、非对象 `state`、非字符串 `clientToken` 和超过 128 字符的 token 都抛固定 `AupuProtocolError`，异常文本不得包含 payload、Topic 或 token。保留所有现有 `parse_shadow_update()` 测试，证明照明语义未改变。

在 `tests/test_wss.py` 增加连接 ready 后的相关 get：

```python
await client.async_request_shadow_get("disc-0123456789abcdef0123456789abcdef")
packet = decode_packets(websocket.sent[-1])[0]
assert packet.topic == "$aws/things/123456789/shadow/get"
assert json.loads(packet.payload) == {
    "clientToken": "disc-0123456789abcdef0123456789abcdef"
}
```

同时断言未订阅、断线、停止和 token 不符合 `disc-[0-9a-f]{32}` 时失败关闭；不会把 publish 排入重连后重放。构造 observer 抛出含合成敏感标记的异常，证明照明先更新、异常不逃出协调器、日志只有 `AUPU discovery observer failed`。

- [ ] **步骤 3：运行红灯测试**

```bash
uv run pytest tests/test_shadow.py tests/test_wss.py tests/test_light.py -q
```

预期：FAIL，缺少 `AcceptedShadow`、相关 get 和发现观察器接口；原有测试仍能被收集。

- [ ] **步骤 4：实现单次 Shadow 解码和照明兼容层**

在 `shadow.py` 增加：

```python
@dataclass(frozen=True, slots=True)
class AcceptedShadow:
    topic_kind: Literal["get", "update"]
    state: dict[str, Any] = field(repr=False)
    client_token: str | None = field(default=None, repr=False)


def parse_accepted_shadow(
    device: DeviceConfig, topic: str, payload: bytes
) -> AcceptedShadow | None:
    topic_kind = _target_topic_kind(device, topic)
    if topic_kind is None:
        return None
    document = _decode_shadow_document(payload)
    state = document.get("state")
    if not isinstance(state, dict):
        raise AupuProtocolError
    client_token = document.get("clientToken")
    if client_token is not None and (
        not isinstance(client_token, str) or len(client_token) > 128
    ):
        raise AupuProtocolError
    return AcceptedShadow(topic_kind, state, client_token)
```

`parse_light_shadow_update()` 只读取 `message.state`，`parse_shadow_update()` 作为现有调用方的兼容包装。`AcceptedShadow.__repr__` 不得展示 `state` 或 client token。

- [ ] **步骤 5：实现仅在活动连接发送的相关 get**

`AupuShadowWebSocket` 保存当前 subscribed websocket 和一个 `asyncio.Lock`，只在两次 SUBACK 成功且首次 `{}` get 已发送后标记 ready。公开方法严格校验 token：

```python
async def async_request_shadow_get(self, client_token: str) -> None:
    if _DISCOVERY_TOKEN.fullmatch(client_token) is None:
        raise AupuProtocolError
    websocket = self._active_websocket
    if websocket is None:
        raise AupuProtocolError
    payload = json.dumps(
        {"clientToken": client_token}, separators=(",", ":")
    ).encode("utf-8")
    async with self._send_lock:
        if websocket is not self._active_websocket:
            raise AupuProtocolError
        await websocket.send_bytes(
            encode_publish(f"$aws/things/{self._device.did}/shadow/get", payload)
        )
```

ping 也通过同一个发送锁；连接清理时先把 `_active_websocket` 置空，再发 disconnected 回调。任何失败都不缓存或自动重放发现 get。

- [ ] **步骤 6：在协调器中隔离双消费者**

协调器接收 `AcceptedShadow`，先运行正式照明解析，再调用可选观察器；观察器异常只写固定日志。协调器提供窄 get 接口和只读 `discovery_available`，后者仅在 runtime 未停止、未进入 reauth、WSS 已 subscribed 时为 true。WSS 不可用时抛固定错误，不暴露传输对象。断线和鉴权失败时调用可选 `cancel` 回调，正式照明原有逻辑保持不变。

```python
@callback
def async_apply_shadow_message(self, message: AcceptedShadow) -> None:
    update = parse_light_shadow_update(self._device, message)
    if update is not None:
        self.async_apply_shadow_update(update)
    observer = self._discovery_observer
    if observer is not None:
        try:
            observer(message)
        except Exception:  # noqa: BLE001 - isolate optional discovery
            _LOGGER.error("AUPU discovery observer failed")
```

- [ ] **步骤 7：运行任务验证**

```bash
uv run pytest tests/test_shadow.py tests/test_wss.py tests/test_light.py -q
uv run ruff check custom_components/aupu_q360/shadow.py custom_components/aupu_q360/wss.py custom_components/aupu_q360/coordinator.py tests/test_shadow.py tests/test_wss.py
uv run mypy custom_components/aupu_q360
```

预期：全部 PASS；连接仍只订阅原有两个 accepted Topic，首次 get 仍为 `{}`，额外 publish 只可能是显式相关 Shadow get。

- [ ] **步骤 8：准备任务提交**

```bash
git add custom_components/aupu_q360/shadow.py custom_components/aupu_q360/wss.py custom_components/aupu_q360/coordinator.py tests/test_shadow.py tests/test_wss.py tests/test_light.py
git diff --cached --check
git diff --cached --stat
```

用户已明确授权本地提交时再运行：

```bash
git commit -m "feat: 增加 Q360 相关只读 Shadow 快照"
```

## Task 2：实现路径和值的 fail-closed 脱敏

**文件：**

- 创建：`custom_components/aupu_q360/discovery_models.py`
- 创建：`custom_components/aupu_q360/discovery_sanitizer.py`
- 创建：`tests/test_discovery_sanitizer.py`

**接口：**

- 产生：`DiscoveryCapability`、`DiscoveryTarget`、`DiscoveryRound`、`DiscoveryState` 固定枚举。
- 产生：`SanitizedValue(kind, comparison, public)`；`comparison` 只用于当前会话且 `repr=False`。
- 产生：每次会话新建的 `DiscoverySanitizer(session_key: bytes, device_id: str)`。
- 产生：`sanitize_reported(state) -> dict[str, SanitizedValue]`。
- 产生：与会话 key 无关的 `validate_discovery_report(report, forbidden_values) -> ScanResult`，供 finish、Store save/load 共用。

- [ ] **步骤 1：编写路径和值脱敏红灯测试**

测试合成 `reported` 同时包含目标设备、其他设备、`desired` 和元数据，只允许目标属性变为：

```python
assert set(snapshot) == {
    "service/2/property/1",
    "service/3/property/7",
}
assert "123456789" not in repr(snapshot)
```

值规则精确锁定为：

```python
assert sanitize(True).public == {"type": "boolean", "value": True}
assert sanitize(12.5).public == {"type": "number", "value": 12.5}
assert sanitize(None).public == {"type": "null", "occurrences": 1}
string_value = sanitize("private-text").public
assert string_value["type"] == "string"
assert string_value["length"] == 12
assert re.fullmatch(r"h-(?:[0-9a-f]{4}-){3}[0-9a-f]{4}", string_value["fingerprint"])
assert sanitize(1_700_000_000).public == {
    "type": "timestamp",
    "precision": "seconds",
}
assert sanitize([1, {"x": 2}]).public == {
    "type": "array",
    "depth": 3,
    "elements": 4,
}
```

同一字符串在同一会话指纹一致，不同 session key 指纹不同；报告/public/repr 均不含原文。Unix 秒范围固定为 `946684800..4102444800`，毫秒范围固定为 `946684800000..4102444800000`，只在 `comparison` 保存当前会话的数值，公开值不保存绝对时间。

参数化拒绝 NaN/Infinity、超过 4 层或 256 个节点的容器、非对象 properties、非十进制或超过 10 字符的 service/property ID。任何错误只得到 `DiscoverySanitizationError("discovery_invalid_payload")`。

- [ ] **步骤 2：运行红灯测试**

```bash
uv run pytest tests/test_discovery_sanitizer.py -v
```

预期：FAIL，新模块不存在。

- [ ] **步骤 3：实现会话内值模型**

`SanitizedValue` 采用自定义 `repr`，只展示 `public`；`comparison` 不参加序列化：

```python
@dataclass(frozen=True, slots=True)
class SanitizedValue:
    kind: ValueKind
    comparison: object = field(repr=False)
    public: JsonObject


class DiscoverySanitizationError(Exception):
    def __init__(self) -> None:
        super().__init__("discovery_invalid_payload")
```

字符串指纹使用随机 32 字节 session key 的 HMAC-SHA256 前 16 个十六进制字符，并固定格式化为 `h-xxxx-xxxx-xxxx-xxxx`，使扫描器不会把连续数字误判为手机号：

```python
digest = hmac.new(
    self._session_key, value.encode("utf-8"), hashlib.sha256
).hexdigest()[:16]
fingerprint = "h-" + "-".join(digest[index : index + 4] for index in range(0, 16, 4))
```

null 在每个快照中从 `occurrences=1` 开始，步骤归并时只增加次数，不引入值。对象/数组只遍历计算结构深度和节点总数，不把键名或叶子值写入 public/comparison；超过限制立即失败关闭。

- [ ] **步骤 4：实现严格目标路径提取与最终扫描**

`sanitize_reported()` 只遍历 `state["reported"][device_id]`；动态设备 ID 在形成任何路径前删除。路径必须匹配 `service/[0-9]{1,10}/property/[0-9]{1,10}`。

最终扫描先验证报告 schema 的固定键、枚举和基础类型，再递归扫描序列化结果，拒绝调用方传入的真实 device ID/entry ID，以及固定敏感标记：`$aws/things/`、`Bearer `、JWT 三段格式、11 位中国手机号、`clientToken`、`payload`、`topic`、`desired`、`signer`。扫描只返回：

```python
ScanResult(passed=True, finding_count=0)
```

命中时抛固定 `DiscoverySanitizationError`，不得返回命中的字段或内容。

- [ ] **步骤 5：运行任务验证**

```bash
uv run pytest tests/test_discovery_sanitizer.py -v
uv run ruff check custom_components/aupu_q360/discovery_models.py custom_components/aupu_q360/discovery_sanitizer.py tests/test_discovery_sanitizer.py
uv run mypy custom_components/aupu_q360/discovery_models.py custom_components/aupu_q360/discovery_sanitizer.py
```

预期：全部 PASS；合成敏感标记在 public、repr、异常和日志中零命中。

- [ ] **步骤 6：准备任务提交**

```bash
git add custom_components/aupu_q360/discovery_models.py custom_components/aupu_q360/discovery_sanitizer.py tests/test_discovery_sanitizer.py
git diff --cached --check
```

获提交授权后：

```bash
git commit -m "feat: 添加 Q360 发现数据脱敏边界"
```

## Task 3：实现步骤差分、候选判定和报告 schema

**文件：**

- 创建：`custom_components/aupu_q360/discovery_analysis.py`
- 修改：`custom_components/aupu_q360/discovery_models.py`
- 创建：`tests/test_discovery_analysis.py`

**接口：**

- 消费：Task 2 的 `SanitizedValue` 与规范化 path。
- 产生：`StepLabel(capability, target, round)`、`StepEvidence`、`CandidateEvidence`。
- 产生：`diff_snapshots(before, after, transient) -> tuple[SanitizedChange, ...]`。
- 产生：`build_discovery_report(...) -> JsonObject`。

- [ ] **步骤 1：编写差分和分类红灯测试**

至少覆盖以下完整矩阵：

- 两轮 `heating:on` 的同一路径以相同 before/after 变化，且两轮 `heating:off` 都恢复会话基线，得到 `confirmed_candidate`。
- 只有一轮、两轮方向不一致、缺少 off、off 未恢复基线、同一路径同时随 heating/ventilation 变化，均得到 `ambiguous`。
- 只在 `idle_environment:off` 观察到的数值为 `observed_unidentified`。
- 能力没有字段变化时生成 path 为 `null` 的 `not_observed`。
- 能力含 invalid 步骤时生成 `invalid`，不能同时升级为 confirmed。
- `desired` 根本不进入输入，不能改变任何分类。
- timestamp 变化只输出 `delta` 和 `direction`，不输出 before/after 绝对值。
- transient 值即使最终恢复也保留变化次数，但每步总数超过 256 时由调用方标 invalid。

核心确认断言：

```python
assert report["candidates"] == [
    {
        "capability": "heating",
        "path": "service/5/property/2",
        "data_type": "boolean",
        "classification": "confirmed_candidate",
        "evidence_steps": [
            "heating:on:1",
            "heating:off:1",
            "heating:on:2",
            "heating:off:2",
        ],
    }
]
```

- [ ] **步骤 2：运行红灯测试**

```bash
uv run pytest tests/test_discovery_analysis.py -v
```

预期：FAIL，分析器和报告模型不存在。

- [ ] **步骤 3：实现差分与瞬时变化归并**

差分按规范路径排序，方向固定为 `added`、`removed`、`increase`、`decrease`、`off_to_on`、`on_to_off`、`changed`。对 timestamp 只输出：

```python
{
    "path": path,
    "data_type": "timestamp",
    "direction": "increase",
    "delta": after.comparison - before.comparison,
    "transient_count": transient_count,
}
```

其他 scalar 使用 `SanitizedValue.public`，对象/数组只输出 shape。相同 path/value 的重复 update 不重复计数；实际变化按接收顺序计数。

- [ ] **步骤 4：实现候选判定**

以 `(capability, target, path)` 聚合两轮证据。只有两个 round 都存在、变化签名相同、path 未关联另一个非 idle 能力，并且同 capability 的两个 off 步骤均 `baseline_restored=True`，才能 confirmed。`idle_environment` 永不 confirmed；跨能力 path 优先 ambiguous；invalid 优先于其他结果；最后按 capability 枚举顺序、path 排序，保证报告稳定。

- [ ] **步骤 5：实现固定报告 schema**

报告顶层精确为：

```python
{
    "schema_version": 1,
    "integration_version": integration_version,
    "session_started_utc_hour": started_at.strftime("%Y-%m-%dT%H:00Z"),
    "wss_baseline_succeeded": True,
    "steps": [...],
    "candidates": [...],
    "limits": {
        "snapshot_timeout_seconds": 10,
        "step_timeout_seconds": 120,
        "session_timeout_seconds": 1200,
        "max_changes_per_step": 256,
        "mqtt_packet_bytes": 65536,
    },
    "statistics": {
        "completed_steps": int,
        "invalid_steps": int,
        "timeouts": int,
    },
    "sanitization_scan": {"passed": True, "finding_count": 0},
}
```

步骤只含受控标签、快照结果、baseline 恢复布尔值和脱敏 changes；不得包含自由文本、异常详情或 client token。

- [ ] **步骤 6：运行任务验证并准备提交**

```bash
uv run pytest tests/test_discovery_analysis.py tests/test_discovery_sanitizer.py -q
uv run ruff check custom_components/aupu_q360/discovery_models.py custom_components/aupu_q360/discovery_analysis.py tests/test_discovery_analysis.py
uv run mypy custom_components/aupu_q360/discovery_models.py custom_components/aupu_q360/discovery_analysis.py
git add custom_components/aupu_q360/discovery_models.py custom_components/aupu_q360/discovery_analysis.py tests/test_discovery_analysis.py
git diff --cached --check
```

获提交授权后：

```bash
git commit -m "feat: 生成 Q360 只读发现候选报告"
```

## Task 4：实现发现会话状态机、超时和清理

**文件：**

- 创建：`custom_components/aupu_q360/discovery.py`
- 修改：`custom_components/aupu_q360/errors.py`
- 创建：`tests/test_discovery.py`

**接口：**

- 消费：`async_request_shadow_get(client_token: str) -> Awaitable[None]`。
- 消费：`async_save_report(report: JsonObject) -> Awaitable[None]`。
- 产生：`StateDiscoverySession.async_start/begin_step/complete_step/finish/cancel/stop`。
- 消费：`sanitizer_factory(session_key: bytes) -> DiscoverySanitizer`，确保每次 start 使用新 HMAC key。
- 消费：`activate_observer(observer, cancel) -> None` 与 `deactivate_observer() -> None`，只在活动会话挂载 coordinator observer。
- 产生：同步回调 `async_observe_shadow(message)`、`cancel_from_transport(error_code)`，供 coordinator/WSS 调用。

- [ ] **步骤 1：编写完整状态机红灯测试**

使用合成 requester 捕获 client token，并把带相同 token 的 `AcceptedShadow(get, ...)` 注入 session。覆盖：

```text
IDLE -> BASELINING -> READY
READY -> STEP_BASELINING -> OBSERVING -> STEP_FINALIZING -> READY
READY -> FINALIZING -> IDLE
任意活动状态 -> CANCELLED -> IDLE
```

每个状态下参数化全部非法 Action，断言 `discovery_invalid_transition` 且合法会话保持；第二次 start 返回 `discovery_busy`。检查每次 get token 匹配 `disc-[0-9a-f]{32}`、只在内存存在、响应 token 不匹配时被忽略。

用可控 clock/sleep 覆盖 10/120/1200 秒边界；快照超时、步骤超时和会话超时分别返回规格错误码并清空 baseline、pending future、step changes、session HMAC key、timer task 和 observer 引用。IDLE 时 observer 未挂载，start 在发 baseline get 前挂载，finish/cancel/失败均精确卸载一次。断线、鉴权失败、卸载、HA stop 走同一清理断言。

- [ ] **步骤 2：运行红灯测试**

```bash
uv run pytest tests/test_discovery.py -v
```

预期：FAIL，状态机不存在。

- [ ] **步骤 3：实现固定错误类和 snapshot waiter**

`errors.py` 增加不接收动态文本的错误：

```python
class DiscoveryError(HomeAssistantError):
    error_code: ClassVar[str]

    def __init__(self) -> None:
        super().__init__(self.error_code)
```

分别定义规格表中的八个 code。session 在发送 get 前建立唯一 pending future；使用 `asyncio.timeout(10)` 等待对应 get accepted。响应必须存在完整 `reported[device_id]`，否则按 `discovery_snapshot_timeout` 取消，不能使用 update/accepted 或不匹配 token 补齐。

- [ ] **步骤 4：实现步骤观察和资源限制**

进入 OBSERVING 后，update/accepted 只提取其中出现的 reported 属性，立即脱敏并与每 path 最近值比较。每个实际变化追加一条；第 257 条停止收集，把当前步骤标为 invalid 并忽略后续 update，但保持 OBSERVING。用户调用 complete 时清理该步骤、回到 READY，并返回固定 `discovery_resource_limit`，从而保留会话又不给不完整证据升级。任何 `DiscoverySanitizationError` 都在 session observer 内转换为当前步骤 `invalid` 并清除该步原始临时值；不得冒泡到 coordinator 或触发 WSS 重连。只有未知编程异常才由 coordinator 的固定日志隔离。

`complete_step()` 再取完整快照，生成 before/after/transient 证据，并把 off 的 after 与会话 baseline 比较。成功后丢弃该步骤的 before/after 临时映射，只留下 `StepEvidence`。

- [ ] **步骤 5：实现 finish、cancel 和幂等 stop**

`finish()` 只在 READY 执行：构造报告、二次扫描、await saver；保存成功后清空，保存失败也清空并抛 `discovery_report_save_failed`。开始新会话不读取或删除旧报告。`cancel()` 从任何活动状态立即清空且不调用 saver；IDLE cancel 返回固定 invalid transition。每个完成、取消或失败出口都调用 `deactivate_observer()`。`async_stop()` 可重复调用，不吞掉外部 task cancellation。

```python
async def async_stop(self) -> None:
    self.cancel_from_transport("discovery_wss_unavailable")
    pending = tuple(self._owned_tasks)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    self._owned_tasks.clear()
```

- [ ] **步骤 6：运行任务验证并准备提交**

```bash
uv run pytest tests/test_discovery.py tests/test_discovery_analysis.py tests/test_discovery_sanitizer.py -q
uv run ruff check custom_components/aupu_q360/discovery.py custom_components/aupu_q360/errors.py tests/test_discovery.py
uv run mypy custom_components/aupu_q360/discovery.py custom_components/aupu_q360/errors.py
git add custom_components/aupu_q360/discovery.py custom_components/aupu_q360/errors.py tests/test_discovery.py
git diff --cached --check
```

获提交授权后：

```bash
git commit -m "feat: 添加 Q360 只读发现会话状态机"
```

## Task 5：实现私有报告存储和诊断下载

**文件：**

- 创建：`custom_components/aupu_q360/discovery_store.py`
- 修改：`custom_components/aupu_q360/diagnostics.py`
- 修改：`custom_components/aupu_q360/__init__.py`
- 修改：`custom_components/aupu_q360/models.py`
- 修改：`tests/test_diagnostics.py`
- 创建：`tests/test_discovery_store.py`

**接口：**

- 产生：`DiscoveryReportStore(hass, entry_id, validate_report)`；validator 不持有 session key。
- 产生：`async_load() -> JsonObject | None`、`async_save(report) -> None`、`async_remove() -> None`。
- 产生：`async_remove_entry(hass, entry) -> None` 清除已删除 Entry 的报告。

- [ ] **步骤 1：编写原子存储红灯测试**

用 fake `Store` 覆盖：首次保存、第二份原子替换、保存异常保留第一份、load 遇到 schema 损坏返回 None、remove 清除。断言构造参数启用 HA 私有和原子写：

```python
Store(
    hass,
    1,
    f"aupu_q360.discovery.{entry_id}",
    private=True,
    atomic_writes=True,
)
```

存储 key 可在 HA `.storage` 内使用 entry ID 定位所属文件，但 JSON report body 不得包含 entry ID。任何 Store 异常日志只允许 `AUPU discovery report storage failed`。

- [ ] **步骤 2：编写诊断红灯测试**

扩展 `_DIAGNOSTIC_KEYS`，无报告时：

```python
"state_discovery": {"report_available": False}
```

有报告时：

```python
"state_discovery": {
    "report_available": True,
    "report": validated_report,
}
```

递归序列化完整诊断，断言现有所有 secret sentinel、device/entry ID、完整 Topic、client token 和原始字符串均不出现；损坏报告必须降级为 `report_available=False`，不能回退读取 Config Entry data。

- [ ] **步骤 3：运行红灯测试**

```bash
uv run pytest tests/test_discovery_store.py tests/test_diagnostics.py -q
```

预期：FAIL，Store 和诊断 report key 不存在。

- [ ] **步骤 4：实现 Store 和 Entry 删除钩子**

每次 save 和 load 都调用 Task 2 的完整 schema/敏感扫描。save 先在内存完成验证，再调用一次 `async_save`；失败不调用 remove。`async_remove_entry()` 通过 `DiscoveryReportStore.async_remove_for_entry(hass, entry_id)` 只构造底层 Store key 并删除，不需要 session sanitizer，不读取 Config Entry data 或报告内容。

在 `AupuRuntimeData` 增加类型化 `discovery_store` 和 `discovery_session` 字段；诊断只通过 runtime store 读取。未加载 runtime 维持安全默认值。

- [ ] **步骤 5：运行任务验证并准备提交**

```bash
uv run pytest tests/test_discovery_store.py tests/test_diagnostics.py tests/test_config_flow.py -q
uv run ruff check custom_components/aupu_q360/discovery_store.py custom_components/aupu_q360/diagnostics.py custom_components/aupu_q360/__init__.py custom_components/aupu_q360/models.py tests/test_discovery_store.py tests/test_diagnostics.py
uv run mypy custom_components/aupu_q360
git add custom_components/aupu_q360/discovery_store.py custom_components/aupu_q360/diagnostics.py custom_components/aupu_q360/__init__.py custom_components/aupu_q360/models.py tests/test_discovery_store.py tests/test_diagnostics.py tests/test_config_flow.py
git diff --cached --check
```

获提交授权后：

```bash
git commit -m "feat: 持久化 Q360 脱敏发现报告"
```

## Task 6：注册 Config Entry 定向 Home Assistant Actions

**文件：**

- 创建：`custom_components/aupu_q360/services.py`
- 创建：`custom_components/aupu_q360/services.yaml`
- 修改：`custom_components/aupu_q360/__init__.py`
- 修改：`custom_components/aupu_q360/models.py`
- 修改：`custom_components/aupu_q360/strings.json`
- 修改：`custom_components/aupu_q360/translations/zh-Hans.json`
- 创建：`tests/test_services.py`
- 修改：`tests/test_config_flow.py`
- 修改：`tests/test_manifest.py`

**接口：**

- 消费：Task 4 的 session API。
- 产生：`async_register_discovery_entry(hass, entry_id)`、`async_unregister_discovery_entry(hass, entry_id)`。
- 产生：五个 `aupu_q360.*_discovery*` Action，所有 handler 返回只含受控 state/message code/count 的 `ServiceResponse`。

- [ ] **步骤 1：编写 schema、注册计数和路由红灯测试**

精确注册以下名称：

```python
START_DISCOVERY = "start_discovery"
BEGIN_DISCOVERY_STEP = "begin_discovery_step"
COMPLETE_DISCOVERY_STEP = "complete_discovery_step"
FINISH_DISCOVERY = "finish_discovery"
CANCEL_DISCOVERY = "cancel_discovery"
```

每个 schema 必须含 `config_entry_id`；begin 另含 capability、target、round。拒绝自由文本、未知 entry、未加载 entry、HTTPS-only/WSS 未 subscribed、非法能力目标矩阵和 round 非 `1/2`。两个 Config Entry 同时加载时服务只注册一次；卸载一个仍保留，卸载最后一个精确移除五个。

Action 响应固定示例：

```python
{
    "state": "observing",
    "message_code": "discovery_ready_for_panel_action",
    "wait_seconds_min": 15,
    "wait_seconds_max": 30,
}
```

finish 只返回 `state=idle`、`report_available=True` 和候选分类计数，不返回报告 body。

- [ ] **步骤 2：运行红灯测试**

```bash
uv run pytest tests/test_services.py tests/test_config_flow.py tests/test_manifest.py -q
```

预期：FAIL，服务模块、services.yaml 和翻译键不存在。

- [ ] **步骤 3：实现域级服务注册表**

在 `hass.data` 中只保存已加载 entry ID 的 set，不保存 Config Entry data。handler 每次通过 `hass.config_entries.async_get_entry()` 解析目标并取 `entry.runtime_data.discovery_session`。首次 entry 注册五个服务，使用 `SupportsResponse.OPTIONAL`；最后 entry 卸载时 `async_remove`。

所有 `DiscoveryError` 转成固定、可翻译的 `ServiceValidationError`，只传 error code，不传原异常文本。服务 handler 不写日志中的 `call.data`。

- [ ] **步骤 4：实现 services.yaml 和双语键**

`services.yaml` 为 config entry 使用 integration selector，为 capability/target/round 使用固定 select selector。英文和简中 `strings.json` 键树完全同构，包含五个服务名、字段名、固定响应 code 和八个错误 code；不得出现真实 ID 格式示例。

- [ ] **步骤 5：组装 runtime 与清理顺序**

`async_setup_entry()` 按以下顺序组装：coordinator（尚未启动）→ report validator/store → session（注入每次会话新建 sanitizer 的 factory、coordinator 的 observer activate/deactivate 和 get 窄接口，但保持 observer 未挂载）→ stoppers（session 在 coordinator 前）→ coordinator start → services register → platforms forward。任一步失败按反序清理。只有 session start 通过 `discovery_available` 前置检查后才生成 HMAC key、挂载 observer 并发送 baseline get。

卸载成功后：先注销该 entry 的服务路由，再 stop session、stop coordinator、删除 runtime_data。HA `EVENT_HOMEASSISTANT_STOP` 只调用 session 固定 cancel；鉴权失败和 WSS disconnect 走 coordinator cancel callback。平台卸载失败时保持 runtime/services 不变。

- [ ] **步骤 6：运行任务验证并准备提交**

```bash
uv run pytest tests/test_services.py tests/test_config_flow.py tests/test_manifest.py tests/test_light.py tests/test_binary_sensor.py -q
uv run ruff check custom_components/aupu_q360/services.py custom_components/aupu_q360/__init__.py custom_components/aupu_q360/models.py tests/test_services.py
uv run mypy custom_components/aupu_q360
uv run python scripts/check_no_secrets.py
git add custom_components/aupu_q360/services.py custom_components/aupu_q360/services.yaml custom_components/aupu_q360/__init__.py custom_components/aupu_q360/models.py custom_components/aupu_q360/strings.json custom_components/aupu_q360/translations/zh-Hans.json tests/test_services.py tests/test_config_flow.py tests/test_manifest.py
git diff --cached --check
```

预期：全部 PASS，秘密扫描 `sensitive_hit_count=0`。

获提交授权后：

```bash
git commit -m "feat: 注册 Q360 只读发现操作"
```

## Task 7：完成真实 HA runtime、文档和本地验收

**文件：**

- 修改：`tests/ha_runtime/test_ha_runtime.py`
- 创建：`docs/q360-read-only-discovery-runbook.md`
- 修改：`README.md`
- 测试：全部 `tests/`

**接口：**

- 消费：Task 1-6 完整功能。
- 产生：可在 Linux 原生 HA pytest runtime 重放的无网络、无凭据测试和操作者 runbook。

- [ ] **步骤 1：扩展真实 HA runtime 测试**

用现有 `_FakeWebSocket` 和真实 HA service registry：加载 WSS entry，断言五个 Action 存在；调用 start 后，从最后一个 MQTT publish 解析 clientToken，再向 fake websocket 注入带相同 token 的合成 get/accepted。按 begin/complete 重复两轮 on/off，finish 后断言：

- 服务响应只含受控字段；
- 灯仍正常处理同一批 reported；
- 诊断包含 `report_available=True` 和 confirmed candidate；
- 报告序列化不含 synthetic device ID、entry ID、token、完整 Topic 或原始字符串；
- unload 后五个服务在最后一个 entry 时移除、报告仍保留；
- 删除 Config Entry 后 Store 报告被清除；
- 所有 `aupu_q360_wss*` 和 discovery timeout task 都不泄漏。

另覆盖 HTTPS-only entry 调 start 得 `discovery_wss_unavailable`，且不产生 MQTT 或 HTTPS 控制调用。

- [ ] **步骤 2：编写运行手册和 README**

runbook 明确区分四阶段：本地验证、获授权同步、获授权重启、获授权真实会话。Action 顺序写为 start → begin → 面板单变量操作 → 等待 15-30 秒 → complete → 两轮开启/关闭 → finish → 下载诊断；取消不会覆盖旧报告。

README 明确发现只发 Shadow get，不发送取暖/换气/烘干/摆风/档位/定时控制；用户的实体面板操作不由 HA 发起；报告不能自动生成实体；原始抓包不是运行依赖。

- [ ] **步骤 3：运行完整 Linux 验证门**

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
git status --short
```

预期：全部 PASS；runtime 使用锁文件中适用于 Python 3.13 的 Home Assistant 2026.2.3 测试依赖；无真实 DNS/TCP、短信、云登录或设备控制；秘密扫描零命中。若 HA runtime 因 Python/uv/平台依赖未运行，不能把本阶段报告为完成。

- [ ] **步骤 4：规格覆盖与占位符自审**

逐节对照规格 1-363 行，建立“规格要求 → Task/测试”清单；运行：

```bash
rg -n 'TO[D]O|TB[D]|待[定]|implement[ ]later|fill[ ]in[ ]details|Similar[ ]to[ ]Task' custom_components tests README.md docs/q360-read-only-discovery-runbook.md
rg -n 'desired|clientToken|\$aws/things/|Bearer ' custom_components/aupu_q360/discovery*.py custom_components/aupu_q360/services.py
```

第一条预期零命中。第二条只允许代码中的显式拒绝/过滤规则与测试合成标记，逐条人工确认不会序列化或日志输出。

- [ ] **步骤 5：准备任务提交**

```bash
git add tests/ha_runtime/test_ha_runtime.py docs/q360-read-only-discovery-runbook.md README.md
git diff --cached --check
git diff --cached --stat
```

获提交授权后：

```bash
git commit -m "docs: 完成 Q360 只读发现验收说明"
```

## Task 8：获单独授权后同步到 HA 并保留可恢复版本

**文件/运行态：**

- 读取：`/home/george/projects/python/ha-aupu-q360/custom_components/aupu_q360`
- 写入（仅获授权后）：容器 `homeassistant:/config/custom_components/aupu_q360`
- 备份（仅获授权后）：容器 `/config/.aupu_q360-backup-<UTC>-<SHA>`

**前置授权：** 本 Task 修改 HA `/config`，不因本地测试通过而自动获准。必须先向用户报告提交 SHA、完整验证结果、当前 live/checkout hash 差异和回滚方案，并获得“允许同步组件”的明确答复；该答复不包含重启授权。

- [ ] **步骤 1：重新核验精确目标和版本**

```bash
git status --short
git rev-parse --verify HEAD
docker ps --filter name=homeassistant --format '{{.Names}}\t{{.Image}}\t{{.Status}}'
docker inspect --format '{{range .Mounts}}{{println .Source "->" .Destination}}{{end}}' homeassistant
docker exec homeassistant python -m homeassistant --version
```

预期：工作区干净、目标容器精确名为 `homeassistant`、挂载精确为 `/home/george/docker/homeassistant/config -> /config`。任一不符即停止，不猜测路径。

- [ ] **步骤 2：只读比较 checkout 与 live 文件清单和 hash**

只比较部署源 `custom_components/aupu_q360` 下的 `.py`、`.json`、`.yaml`、`.png`，跳过 live `__pycache__`，输出文件名和 hash，不输出文件内容。确认部署文件清单不包含仓库根目录的 `config_entry-*.json`、`.private`、HAR、Cookie 或证书；不读取这些排除项。

- [ ] **步骤 3：在容器 `/config` 内建立候选和备份后原子换名**

执行时先把 HEAD 完整 SHA 和 UTC 时间解析到任务专用变量，并验证 SHA 为 40 位小写十六进制、时间为 `YYYYMMDDTHHMMSSZ`；任何验证失败停止。候选目录必须预先确认不存在。获同步授权后依次：

1. `docker cp` checkout 的 `custom_components/aupu_q360/.` 到 `/config/.aupu_q360-candidate-<SHA>/`；
2. 比较候选与 checkout 的允许文件 hash；
3. 把现有 `/config/custom_components/aupu_q360` 移到 `/config/.aupu_q360-backup-<UTC>-<SHA>/`；
4. 把候选目录在同一文件系统内改名为 `/config/custom_components/aupu_q360`；
5. 不删除 backup、失败候选或旧 `__pycache__`。

不要使用 `rsync --delete`、递归删除、通配符删除或直接覆盖 live 目录。

- [ ] **步骤 4：在不重启的情况下检查配置**

```bash
docker exec homeassistant python -m homeassistant --script check_config -c /config
```

预期：PASS。失败时在同一同步授权范围内，把新目录改名为 `.aupu_q360-failed-<UTC>-<SHA>`，再把备份原子改回 live；重新运行 config check 并报告。成功时停止并报告“文件已同步但运行中 HA 尚未加载新代码”，等待独立重启授权。

- [ ] **步骤 5：获独立重启授权后加载并烟测**

只有用户明确允许重启后执行容器重启。记录 Asia/Shanghai 与 UTC 时间，确认容器恢复运行、HA 版本未变化、集成加载无固定错误、原照明和状态通道实体仍存在。不得自动开始 discovery，也不得调用浴霸控制。

若启动回归：恢复 backup，重新 config check，并再次获得重启授权后才重启到旧版本。不得删除失败版本，直到用户确认验收结束。

## Task 9：获单独授权后执行一次真实只读发现会话

**运行态：** Home Assistant 开发者工具 → 操作、实体面板、集成诊断下载。

**前置授权：** 用户明确允许“真实 Q360 发现会话”；同步和重启授权不包含本 Task。会话前确认 live 允许文件 hash 与已验证 checkout HEAD 一致，并在 HA 配置目录外的受限位置完成 Config Entry 备份；备份包含凭据，不进入 Git、聊天或 shell 输出。

- [ ] **步骤 1：确认安全基线**

用户在实体面板把取暖、换气、烘干、摆风、档位和定时全部关闭；HA 中 WSS 状态通道为 connected。调用 `aupu_q360.start_discovery`，若 10 秒内未返回 READY，停止并保留旧报告，不反复重试。

- [ ] **步骤 2：按单变量矩阵完成两轮**

对 `heating`、`ventilation`、`drying`、`swing`、`timer`：每轮依次 begin(on) → 只在实体面板开启 → 等待 15-30 秒 → complete → begin(off) → 面板关闭 → 等待 → complete。

对 `fan_level`：只对实体面板实际可见的档位使用 `level_1/2/3`，每个可见档位完成两轮；每次档位观察后执行同轮 off 恢复基线。不存在的档位不猜测、不操作。

对 `idle_environment`：在全部功能关闭时执行两轮 `target=off`，每轮观察 15-30 秒，不改变任何面板功能。环境数值只能得到 `observed_unidentified`，除非用户另有同一时刻的奥普界面数值证据。

任何误操作、多变量同时变化、断线、超时或资源限制都取消本次会话；不使用不可靠步骤凑齐两轮。

- [ ] **步骤 3：finish、下载并审查脱敏报告**

没有活动步骤时调用 finish，下载集成诊断。先验证报告中以下内容零命中：真实 device/tag/entry/entity ID、JWT、签名、手机号、完整 Topic、client token、原始字符串、原始 Shadow。命中即停止，不分享报告，并回滚到本地缺陷修复流程。

再由用户审查 `confirmed_candidate`、`ambiguous`、`observed_unidentified`、`not_observed`、`invalid`。不得从 property 编号、数值范围或时间相关性单独推断业务语义。

- [ ] **步骤 4：结束边界**

本 Task 只交付脱敏报告和人工确认清单；不修改 Config Entry 映射、不新增 binary_sensor/sensor、不发送新控制、不 push 或发布。确认字段进入下一轮独立规格和实施计划。

## 最终完成标准

- 所有 Task 1-7 测试、lint、format、mypy、秘密扫描和 Linux HA runtime 门均有当次 PASS 证据。
- 现有照明解析、HTTPS 控制、WSS 单连接、心跳、重连、状态通道和 reauth 测试无回归。
- 未活动 discovery 时不保留额外 Shadow 状态；活动时所有原始内容在完成/取消/失败路径释放。
- 最近报告只有在完整脱敏和原子保存成功后替换；诊断不存在公开 URL。
- Task 8/9 未获授权时明确标记为未执行，不影响“本地实现与自动化验证完成”的准确表述，但不能声称已部署或已发现真实字段。
- 不需要原始抓包文件完成 Task 1-8；只有厂商协议或现有 WSS/Shadow 结构与合成契约不符时，才停止并向用户申请一份受控、只读、离线分析材料，且材料不得进入 Git、计划文档或输出。
