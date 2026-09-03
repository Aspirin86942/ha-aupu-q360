# Q360 状态发现消融设计

## 文档状态

- 日期：2026-09-03。
- 状态：方案 A 已确认；本文只定义消融后的临时探针和后续边界。
- 当前基线：`main` 的 `0.2.4`，提交 `eb4f8ba`。
- 适用范围：本仓库的 Q360 面板状态发现代码、测试、HA Action、用户文档及其运行时接线。
- 关系：本文取代现有 v2 发现系统的实现方向，但不把尚未观测到的字段写成正式映射。

## 背景与结论

当前 v2 发现系统为一次性字段识别构建了长期产品级基础设施：五个 Action、固定实验目录、阶段
状态机、恢复判定、HMAC 脱敏、schema 2 报告、HA Store、Diagnostics、可选原始档案和 Compose
挂载。生产发现相关模块约 3,800 行，专项测试约 3,500 行；完整实验包含 26 个周期和约 70 次
人工阶段操作。

真实手机实验已经证明现有链路可以取得关联 Shadow `reported`，也暴露了问题：产品内状态机
试图解释远程操作者的每一步，恢复判断会因背景字段或多字段变化阻断下一步，而真正目标只是
短期找出少量字段路径。

采用方案 A：把发现功能降级为一次性、只读、内存内的开发探针。探针只做关联快照和相邻快照
差异；实验含义由仓库外的人工流程关联。取得足够证据并实现正式字段映射后，最终正式集成必须
删除探针，不把它作为永久调试接口发布。

## 目标

1. 把五个发现 Action 缩减为 `start_probe`、`sample_probe`、`stop_probe` 三个 Action。
2. 继续复用当前唯一 AWS IoT MQTT-over-WSS 连接，只发送带受控 token 的 Shadow `get`。
3. 只消费目标设备 `get/accepted` 中与当前请求关联的 `state.reported`。
4. Action 响应只显示规范化 `service/<n>/property/<n>` 路径以及布尔值或小整数的变化。
5. 断线、超时、显式停止、Config Entry unload 和 HA stop 都清空探针内存并移除 observer。
6. 删除实验目录、阶段状态机、恢复判定、报告 schema、HA Store、Diagnostics 报告和原始档案。
7. 用一次自适应手机实验取得事实证据，再另写包含精确字段的正式映射规格和实施计划。

## 非目标

- 不自动操作实体面板、奥普官方 App、微信小程序、HA 实体或任何设备控制接口。
- 不发布 Shadow `update`，不写 `desired`，不创建第二条 WSS。
- 不接受实验名、轮次、阶段、标签、备注、目标值或任意用户文本。
- 不在集成中判断“模式已恢复”“实验已完成”或“字段已确认”。
- 不保存快照、变化、样本、报告或原始 topic/payload 到磁盘、HA Store、Diagnostics 或日志。
- 不保留 HMAC、指纹、原始档案、报告 schema、覆盖率或候选分类。
- 不依据未知字段创建实体，不在本计划中填写猜测路径。
- 不把临时探针推送、打 tag 或发布为最终版本。

## 采用方案与未采用方案

### 采用：相邻快照内存探针

`start_probe` 续建现有 WSS 并取得一份关联 `reported` 基线；每次 `sample_probe` 再取得一份关联
快照，返回它与上一份快照之间的允许字段差异，然后用新快照替换内存基线；`stop_probe` 只清理
内存和 observer。外部操作者按指令在手机上一次只改变一个变量，Action 本身不知道变量名称。

这条路径直接回答“刚才的手机操作让哪些安全标量字段改变”，无需在 HA 内复制实验编排系统。

### 未采用：精简现有 v2 状态机

保留目录、阶段和恢复语义仍需要目录模型、状态转换、超时、候选分析及报告校验，复杂度下降
有限，且会继续把手机 UI 状态与 Shadow 字段证据耦合。

### 未采用：保存原始 Shadow 后离线分析

原始数据会重新引入私有目录、权限、容量、清理、Compose 挂载和敏感材料授权，正是本次要
消除的主要负担。

## 架构

```text
手机人工操作 ──> 奥普官方云 ──> Q360

HA start/sample Action
        │
        ├─> 当前 AupuCoordinator
        │      └─> 当前唯一 AupuShadowWebSocket
        │              └─> Shadow get {clientToken: "disc-<32 hex>"}
        │
        └─> PanelStateProbe observer
               └─> 只接受匹配 token 的 get/accepted state.reported
                       └─> 规范化布尔/小整数快照
                               └─> 与上一快照求差并立即返回
```

正式灯光解析仍在 `AupuCoordinator.async_apply_shadow_message()` 中先执行，探针 observer 后执行。
observer 异常不得阻断灯光状态、connectivity、WSS keepalive 或重连。

## 临时探针接口

### Action 输入

三个 Action 都只接受一个字段：

```yaml
config_entry_id:
  required: true
  selector:
    config_entry:
      integration: aupu_q360
```

不得增加自由文本、标签、实验名、轮次、阶段或设备值。目标 Config Entry 必须已加载且启用了
WSS。

### Action 输出

三个 Action 使用同一个固定响应形状：

```python
type ProbeValue = bool | int
type PublicProbeValue = ProbeValue | None
type ProbeChange = dict[str, str | PublicProbeValue]
type ProbeResponse = dict[str, str | int | list[ProbeChange]]
```

`start_probe` 成功：

```json
{
  "state": "active",
  "message_code": "probe_started",
  "sample_count": 0,
  "changes": []
}
```

`sample_probe` 成功时按路径排序；`null` 只表示允许的标量路径在前后某一侧缺失：

```json
{
  "state": "active",
  "message_code": "probe_sampled",
  "sample_count": 1,
  "changes": [
    {
      "path": "service/6/property/2",
      "before": 3,
      "after": 4
    }
  ]
}
```

没有变化时 `changes` 是空数组，仍把 `sample_count` 加一并更新基线。`stop_probe` 返回清理前的
样本数，然后清空内存：

```json
{
  "state": "inactive",
  "message_code": "probe_stopped",
  "sample_count": 1,
  "changes": []
}
```

在没有活动探针时调用 `stop_probe` 是幂等成功，返回 `sample_count: 0`。这使异常收尾不需要先
查询内部状态。

### Python 表面

新增 `custom_components/aupu_q360/probe.py`，公开的运行时对象只有
`PanelStateProbe`。它提供六个精确方法：`async_start() -> ProbeResponse`、
`async_sample() -> ProbeResponse`、`async_stop_probe() -> ProbeResponse`、
`async_stop() -> None`、`observe_shadow(message: AcceptedShadow) -> None` 和
`cancel_from_transport() -> None`。

`async_stop_probe()` 服务于 HA Action；`async_stop()` 服务于现有 `AsyncStopper` 生命周期。两者
共享同一个幂等清理函数。

## 快照提取与差异规则

1. 只读取 `state["reported"][device_id]`。缺失或不是对象时，本次请求以
   `probe_invalid_payload` 失败并停止探针。
2. service id 和 property id 必须是 1 至 10 位 ASCII 十进制字符串。
3. 只遍历 `<service>["properties"]`；没有 `properties` 的 service 忽略，存在但不是对象则失败。
4. 允许值仅为：
   - Python 精确 `bool`；
   - 排除 `bool` 后，闭区间 `-1000..1000` 内的 Python 精确 `int`。
5. 字符串、浮点数、`null`、对象、数组、大整数和时间戳全部忽略，不散列、不指纹化。
6. 每份快照最多保留 256 个允许路径；每次响应最多 128 个变化。超限不截断，固定失败并停止。
7. 差异取前后路径并集；值相等不输出，新增/消失路径的一侧输出 `null`，结果按路径排序。
8. 内存快照不包含 device id、topic、client token、原始 payload 或 `AcceptedShadow` 对象。

这些规则允许直接观察布尔模式与常见档位/温度整数，同时自然排除 Unix 时间戳、标识字符串和
嵌套私有内容。

## 关联、状态与并发

探针只有 `inactive` 和 `active` 两个可观察状态，不存在 phase、cycle、ready、restore 或
finalizing。

`start_probe` 固定顺序：

1. 若已 active，返回 `probe_busy`。
2. 调用现有 WSS 的 `async_renew_and_wait_healthy(45.0)`；任何失败映射为
   `probe_wss_unavailable`。
3. 挂载当前 Config Entry 唯一 probe observer。
4. 生成 `disc-` 加 32 位小写十六进制 token，发送一次 Shadow `get`。
5. 最多等待 10 秒，只接受 `topic_kind == "get"` 且 token 完全匹配的响应。
6. 提取基线，清除 pending token/future，返回 `probe_started`。

`sample_probe` 重复步骤 4 至 6，计算相邻差异并替换基线。未关联的 `update/accepted`、旧 token、
其他 Config Entry 或不含目标 `reported` 根的消息都不能完成 pending future。

同一 Config Entry 同时只允许一个 start/sample 操作；并发调用返回 `probe_busy`，不排队形成
无法对应手机动作的隐式样本。不同 Config Entry 各自拥有独立探针，但域级 Action 注册仍只做
一次。

## 失败与清理

固定错误码只有五个：

| 错误码 | 条件 |
| --- | --- |
| `probe_busy` | 已 active，或同一 entry 正在 start/sample |
| `probe_inactive` | inactive 时调用 `sample_probe` |
| `probe_wss_unavailable` | WSS 未启用、续建失败、断线或鉴权失败 |
| `probe_snapshot_timeout` | 10 秒内没有关联完整快照 |
| `probe_invalid_payload` | 目标 reported 结构无效或超过固定资源上限 |

以下路径都必须执行相同清理：

- `stop_probe`；
- `async_stop`；
- WSS 断开或鉴权失败触发 `cancel_from_transport`；
- start/sample 超时；
- 请求发送失败；
- payload 校验或资源限制失败；
- Config Entry unload；
- HA stop。

清理会取消 pending future、清除 token/快照/计数、标记 inactive 并移除 observer。清理不发送
设备命令，不恢复手机状态，不创建任务重试，也不保留“最后错误”或“手工恢复需要”状态。
Action 错误只通过 HA 的 `ServiceValidationError` 和固定翻译 key 返回；日志不得包含路径、值、
token、topic 或 payload。

## 删除与保留矩阵

### 立即删除的生产模块

| 文件 | 处理 | 原因 |
| --- | --- | --- |
| `discovery.py` | 删除 | 918 行阶段状态机、周期证据和恢复门不再需要 |
| `discovery_analysis.py` | 删除 | 不再生成候选、覆盖率和恢复分析 |
| `discovery_catalog.py` | 删除 | Action 不再接受实验目录或轮次 |
| `discovery_models.py` | 删除 | v2 phase/cycle/report 模型不再存在 |
| `discovery_report_schema.py` | 删除 | 不再生成或读取 schema 2 报告 |
| `discovery_sanitizer.py` | 删除 | 不再保留字符串/对象指纹或 HMAC key |
| `discovery_store.py` | 删除 | 探针不持久化到 HA Store |
| `raw_discovery_archive.py` | 删除 | 不保留原始事件、manifest 或私有文件 |

对应专项测试删除，改由 `tests/test_probe.py` 与
`tests/test_probe_network_boundary.py` 覆盖新的小接口。历史 specs/plans 可保留为决策记录，但不再
代表当前运行接口。

### 修改的接线文件

| 文件 | 处理 |
| --- | --- |
| `__init__.py` | 只构造 `PanelStateProbe`，挂到 runtime/stoppers，注册三个 Action；删除 Store、archive 和 report validator，并把 HA stop listener 缩为 probe 内存清理 |
| `models.py` | `discovery_session` 改为 `probe`；删除 `discovery_store`、`raw_archive_enabled`，加载旧 entry 时静默忽略旧 key |
| `services.py` | 五 Action 改三 Action；三个 schema 都只有 `config_entry_id`；删除目录解析和报告计数 |
| `coordinator.py` | observer/cancel 改为 probe 命名；保留正式灯光优先顺序、续建 WSS、关联 get 和断线取消 |
| `wss.py` | 保留 token 校验与 get 发布；删除只为原始档案存在的 outgoing recorder |
| `shadow.py` | `AcceptedShadow` 保留已解析 state/token；删除只为档案存在的 `RawShadowEvent` 和 raw_event |
| `config_flow.py` | 删除原始档案 Options；不因升级主动改写 Config Entry |
| `diagnostics.py` | 删除 `state_discovery`，只保留既有连接、认证和灯光健康白名单 |
| `errors.py` | 删除全部 `Discovery*` 类；五个 probe 错误在 `probe.py` 内使用受控联合类型和单一异常类 |
| `services.yaml`、`strings.json`、`zh-Hans.json` | 只公开三个 probe Action 和五个固定错误翻译 |
| `README.md`、运行手册 | 改为临时探针说明和自适应实验，不再描述 v2 报告、档案或 26 周期目录 |

### 保留且不得退化的正式能力

- `AupuShadowWebSocket` 的唯一连接、连接续建、keepalive、包大小限制和受控 Shadow get。
- `parse_accepted_shadow()` 的目标 topic 校验和 JSON 协议校验。
- `parse_light_shadow_update()` 及正式照明路径 `state.reported.<device-id>.2.properties.1`。
- `AupuCoordinator` 的认证、Repair、灯光控制、connectivity、状态陈旧语义和监听器。
- Config Flow、短信 Reauth、手工 Token、HACS 元数据、私有签名检查和敏感信息扫描。

### 运行环境中的旧状态

代码升级后不再读取或写入旧 discovery Store，也不再访问原始档案挂载。为避免升级本身产生
额外状态变更：

- 不自动删除旧 Store 文件；它们变成不可达的历史脱敏数据，最终清理部署时按精确 key 处理。
- 不自动改写 Config Entry；旧 `raw_archive_enabled` 输入 key 被忽略，下一次正常保存配置时自然
  丢弃。
- 不由集成修改 Compose 或宿主机目录；最终清理部署在重新核实空目录、挂载和备份后处理。

## 自适应手机实验

实验编排留在集成外。操作者只在奥普官方 App 或官方微信小程序执行收到的单变量指令；HA 端
按顺序调用 Action。建议最小流程：

1. 确认现场安全、自动化暂停、所有模式关闭，并私下记住原档位和原温度。
2. 调用 `start_probe` 建基线，再在不操作手机时调用一次 `sample_probe` 观察背景变化。
3. 七个模式逐个执行“开启、sample、关闭、sample”。
4. 以换气为载体，执行“开启并 sample、改一个非原档并 sample、恢复原档并 sample、关闭并
   sample”；只有差异不唯一时再选第二个目标档复核。
5. 以 AI 恒温暖为载体，对目标温度执行同样流程；只有差异不唯一时再选第二个合法温度复核。
6. 每项候选必须同时看到变化和恢复；背景样本中出现的路径不能直接确认。
7. 调用 `stop_probe`，在官方控制面确认模式、档位和温度恢复；异常时由家中人员现场确认。

基准流程约 23 次 sample，而不是固定 70 次阶段操作。遇到多个变化路径时，只针对该能力增加
一轮定向复核，不要求所有能力无条件重复两轮。

安全的 Action 响应可以在当前受控开发会话中用于人工关联，但不得复制 device id、token、原始
topic/payload 或官方控制面私有材料。探针本身不生成证据文件。

### 无结果时的降级条件

“不需要抓包”只适用于目标能力确实以布尔值或小整数回写 Shadow `reported` 的情况。探针连续
两次单变量操作均返回空差异，或只返回无法从变化/恢复配对中排除的多个路径时，必须把该能力
标记为“Shadow 探针不可确认”并停止猜测。此时才另行设计和授权奥普 App 层的短期抓包或其他
观测方案；外部 TLS 抓包不是当前计划的默认依赖，也不能替代解密后的应用层证据。

## 两个计划边界

### 当前计划：临时探针消融

当前实施计划只允许使用已知接口和合成路径完成以下结果：

- 建立并测试三 Action 内存探针；
- 删除旧 v2 发现系统和持久化/档案接线；
- 本地完整验证；
- 经运行态授权临时同步到 HA；
- 经真实实验授权执行自适应手机实验并收集脱敏差异。

当前计划不创建模式、档位或温度实体，也不写入任何未知真实字段占位。

### 后续计划：正式字段映射与探针删除

只有真实实验给出可复核的精确路径、类型、值域和恢复证据后，才新建正式映射规格与实施计划。
后续计划必须：

1. 写入真实证据支持的精确字段路径与只读/控制语义；
2. 为正式实体和 coordinator 解析增加合成测试；
3. 删除 `probe.py`、三个 probe Action、翻译、运行手册和专项测试；
4. 移除旧 Compose 挂载、空私有目录和精确旧 Store key；
5. 通过完整验证后才升级版本、推送或发布。

如果实验不足以确认字段，后续计划只能缩小正式映射范围，不能保留探针作为默认长期功能。

## 授权与交付边界

产品设计不再设置原来的六个 discovery 内部授权门。执行流程压缩为三个清晰范围：

1. **临时部署**：本地探针提交通过完整验证后，备份并同步组件、检查 HA 配置并重启加载；不
   推送、不发布、不改 Compose、不读凭据。
2. **手机实验**：重新核实 WSS 与现场安全后，HA 只调用三个 probe Action，用户只操作官方
   手机控制面；不读取 Config Entry 凭据、不控制设备。
3. **最终清理部署**：在后续正式映射计划完成后，删除探针及遗留 Store/mount/空目录，完整
   验证、同步和重启；最终代码确认不含探针后才可推送/发布。

这些是工作范围而不是绕过系统安全边界的永久许可。实际提交、运行态同步、HA 重启、外部推送
和数据删除仍按全局规则在执行前确认精确目标与当前状态；一次确认可覆盖同一范围内已列明的
连续步骤，不再为每个内部函数或人工阶段重复询问。

## 测试策略

1. 纯函数测试覆盖 reported 目标根、路径规范化、允许值、忽略值、前后并集、确定性排序和上限。
2. 异步单元测试覆盖 start/sample/stop、token 关联、超时、并发、断线、异常和每条清理路径。
3. 服务测试覆盖三个 Action 只接受 `config_entry_id`、多 Config Entry 路由、固定响应与翻译错误。
4. 网络边界测试证明只使用当前 WSS、只发布 Shadow get、没有 HTTP 控制或 Shadow update。
5. 集成接线测试证明正式灯光先处理，探针失败被隔离，unload/HA stop 清理且不持久化。
6. 删除断言证明生产包不再含旧八个模块、五个旧 Action、raw archive option、Diagnostics 报告或
   `RawShadowEvent`。
7. 完整运行 pytest、HA runtime、Ruff、format、mypy、lock、私有 signer、敏感扫描和
   `git diff --check`。

所有自动测试只用合成数据和内存 WSS，不访问真实账号或设备。测试通过不证明任何未知字段
语义，也不授权临时部署或手机实验。

## 完成标准

1. 生产发现表面从五 Action 和八个专用模块缩减为三 Action 和一个 `probe.py`。
2. 探针只返回规范路径与布尔/小整数变化，所有数据只存在于内存和单次 Action 响应中。
3. 旧报告、Store、Diagnostics、原始档案和 Config Entry 选项不再由运行代码访问。
4. 唯一 WSS、关联 get、正式灯光优先与断线清理均有测试证据。
5. README 和运行手册明确探针是临时开发工具，不是正式发布功能。
6. 自适应实验能以约 23 次基准 sample 获取候选，并只对模糊项追加复核。
7. 当前计划不猜测字段；正式映射与探针删除由取得真实证据后的独立计划完成。
