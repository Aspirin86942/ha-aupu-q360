# Q360 只读状态发现设计

## 背景

现有集成通过持续 AWS IoT WSS 通道接收 Device Shadow，并将
`state.reported.<device-id>.2.properties.1` 映射为照明确认状态。Shadow 消息可能还包含
取暖、换气、烘干、摆风、档位、定时、温度、湿度、功率或设备健康字段，但当前解析器会忽略
未经验证的路径。

本设计增加一次性的只读发现会话，通过用户在浴霸实体面板执行单变量实验，识别完整
`reported` 状态中的候选字段。发现功能不控制除现有照明以外的任何能力，也不根据字段编号或
数值范围猜测业务含义。

## 已确认决策

- 同时观察功能运行状态和环境、设备数据。
- 第一版严格只读，不增加取暖、换气、烘干、摆风或档位控制。
- 用户配合约 15 至 20 分钟的实体面板单变量实验。
- 未确认字段不创建临时或正式 Home Assistant 实体。
- 原始 Shadow 只在内存中处理，不写日志、不落盘。
- 会话结束后只保留最近一次脱敏报告。
- Shadow 未提供温度、湿度或功率时，只报告本次未观察到，不引入外部传感器。
- 代码后续迁移到安装有 Home Assistant 的 Linux 主机本地开发；规格本身不执行迁移、部署或
  凭据复制。

## 目标

1. 复用现有 WSS 连接，取得完整的 `state.reported` 基线和步骤快照。
2. 将每次实体面板操作与 Shadow 字段变化建立可审计的证据关系。
3. 对两轮一致、可恢复基线的字段生成 `confirmed_candidate`，供用户人工确认。
4. 生成不含设备标识、凭据、原始 Topic 和原始载荷的脱敏报告。
5. 保证发现功能的失败不会改变照明实体、状态通道或 WSS 重连行为。

## 非目标

- 不发送取暖、换气、烘干、摆风、档位或定时控制。
- 不自动生成未知实体，不自动写入正式状态映射。
- 不持久化原始 Shadow、操作中的临时快照或高频状态历史。
- 不通过 `desired` 推断设备已经执行命令。
- 不根据看似合理的数值范围将未知字段命名为温度、湿度或功率。
- 不新增第二条 MQTT/WSS 连接，不探测局域网协议，也不恢复原始抓包。
- 不在本次规格中设计外部电表、温湿度传感器或其他硬件。

## 总体架构

```text
现有 WSS Shadow 消息
        │
        ├── 原有照明解析器 → AupuCoordinator → 正式照明实体
        │
        └── 发现观察器（仅活动会话期间）
                → 提取 reported
                → 路径和值脱敏
                → 内存快照与步骤差分
                → 候选判定
                → 最近一次脱敏报告
```

现有 `AupuShadowWebSocket` 仍是唯一传输层。它只对已经通过 Topic、MQTT 包大小和 JSON 边界
校验的 `get/accepted`、`update/accepted` 消息调用可选发现观察器。发现观察器未启用时没有
额外状态处理。

正式照明解析器与发现观察器是两个隔离的消费者：正式解析结果继续交给
`AupuCoordinator`；发现解析异常只能使当前发现步骤无效，不得阻止照明更新、关闭 WSS 或
触发重连。

## 组件职责

### `AupuShadowWebSocket`

- 维持现有单一 WSS 连接、订阅、心跳和重连策略。
- 在既有两个 accepted Topic 上接收完整载荷。
- 提供只读 Shadow `get` 请求能力，供发现会话按步骤取得一致快照。
- 将符合边界的消息分流给正式解析器和可选发现观察器。
- 不保存发现会话、字段映射或报告。

### `AupuCoordinator`

- 继续作为正式照明状态、来源、过期性和确认时间的唯一权威。
- 暴露 WSS 是否可用以及只读 Shadow `get` 的窄接口。
- 不解释未知字段，不持有原始报告。

### `StateDiscoverySession`

- 每个 Config Entry 同时最多存在一个实例。
- 管理会话状态、20 分钟总超时、步骤标签和两轮实验。
- 保存当前会话所需的内存快照和已脱敏变化。
- 在完成、取消、断线、鉴权失败、卸载或 HA 停止时清理原始内存。

### `DiscoverySanitizer`

- 只读取目标设备的 `state.reported` 分支。
- 在差分前删除动态设备 ID，并将路径规范化。
- 按类型处理值，阻止字符串原文、原始时间戳和嵌套内容进入报告。
- 对最终报告执行第二次禁止字段扫描。

### `DiscoveryReportStore`

- 只保存经过完整脱敏校验的最近一次报告。
- 使用 Home Assistant 管理的本地持久化机制进行原子替换。
- 保存失败时保留旧报告，不产生半写文件。
- Config Entry 删除时清除所属报告。
- 将报告加入现有 Config Entry 诊断下载，不建立公开下载 URL。

## Home Assistant 操作接口

第一版不开发自定义前端。通过“开发者工具 → 操作”注册以下 Config Entry 定向操作：

- `aupu_q360.start_discovery`
- `aupu_q360.begin_discovery_step`
- `aupu_q360.complete_discovery_step`
- `aupu_q360.finish_discovery`
- `aupu_q360.cancel_discovery`

所有操作都必须明确指定目标 Config Entry。操作不能接受自由文本名称；步骤能力、目标状态和
轮次使用受控枚举，避免任意文字进入报告。

预定义能力至少包括：

- `heating`
- `ventilation`
- `drying`
- `swing`
- `fan_level`
- `timer`
- `idle_environment`

目标状态由 `off`、`on` 或预定义档位构成；轮次只能是 `1` 或 `2`。

## 会话流程

### 1. 开始会话

`start_discovery` 检查以下前置条件：

- Config Entry 已加载；
- WSS 已启用并完成订阅；
- 当前不存在活动会话；
- 集成不处于 Reauth 或停止状态。

通过后发送一次只读 Shadow `get`。必须在 10 秒内收到对应 `get/accepted` 的完整
`reported`，否则会话取消。成功快照成为“全部关闭”基线。

### 2. 开始实验步骤

`begin_discovery_step` 接受能力、目标状态和轮次。系统先发送 Shadow `get` 并取得操作前
快照，随后进入观察状态。用户收到可以操作实体面板的明确提示。

步骤开始后，发现会话收集该步骤期间所有 `reported` 变化。正式照明解析继续照常处理同一
批消息。

### 3. 完成实验步骤

用户操作实体面板并等待 15 至 30 秒后执行 `complete_discovery_step`。系统再次发送 Shadow
`get`，取得操作后快照，并计算：

- 操作前后的字段差异；
- 步骤期间出现的瞬时变化；
- 值类型和变化方向；
- 关闭步骤是否恢复到会话基线。

步骤完成后回到等待状态。单个步骤最多持续 2 分钟。

### 4. 重复验证

每项能力至少完成两轮开启和关闭。档位类能力需要每个计划映射的档位完成两轮。单次变化或
没有关闭回归证据的字段不能成为确认候选。

### 5. 完成或取消

`finish_discovery` 只在没有活动步骤时可执行。它构造报告、进行最终脱敏扫描、原子替换最近
一次报告，然后清除全部原始会话数据。

`cancel_discovery` 不生成报告，立即清除原始快照和临时变化。会话超时、WSS 断开、鉴权
失败、集成卸载和 HA 停止等同于取消。

## 状态机

```text
IDLE
  └─ start ─> BASELINING
                 ├─ baseline accepted ─> READY
                 └─ timeout/disconnect ─> CANCELLED ─> IDLE

READY
  ├─ begin step ─> STEP_BASELINING ─> OBSERVING
  │                                      ├─ complete ─> STEP_FINALIZING ─> READY
  │                                      └─ timeout/disconnect ─> CANCELLED ─> IDLE
  ├─ finish ─> FINALIZING ─> IDLE
  └─ cancel ─> CANCELLED ─> IDLE
```

不符合当前状态的操作失败关闭，不隐式跳步或自动补快照。

## 路径与值脱敏

发现模块只检查：

```text
state.reported.<device-id>.<service-id>.properties.<property-id>
```

报告路径统一表示为：

```text
service/<service-id>/property/<property-id>
```

值处理规则：

- 布尔值和普通有限数值保留实际值。
- 字符串不保存原文，只保存长度和会话内稳定指纹。
- 疑似 Unix 秒或毫秒时间戳只保存相对变化，不保留载荷中的绝对值。
- `null` 只记录类型和出现次数。
- 对象和数组只记录结构类型、深度和元素数量，不展开内容。
- 非有限数值、超深结构、超长键或非法属性容器将对应步骤标为 `invalid`。

MQTT 包继续受现有 64 KiB 上限约束。单个步骤最多保留 256 条已脱敏变化；超过上限时停止
该步骤收集并标记为 `invalid`，但不影响正式状态通道。

## 候选判定

每个能力与字段组合只能得到以下结果之一：

- `confirmed_candidate`：两轮变化方向一致、均有 `reported` 证据，且关闭后恢复基线。
- `ambiguous`：字段发生变化，但两轮不一致、同时关联多个能力或没有恢复基线。
- `observed_unidentified`：发现了数值或枚举变化，但没有足够证据证明业务含义。
- `not_observed`：本次实验没有观察到相关变化，不代表设备永远不支持。
- `invalid`：输入结构、资源上限或实验步骤不满足安全分析要求。

`desired` 可以用于指出云端目标与设备报告存在差异，但不能提高候选可信度。功能字段编号、
数值范围和变化时间都不能单独作为业务命名依据。

温度、湿度和功率必须有独立语义证据才能成为正式候选，例如奥普界面在同一时刻展示相同
数值。没有此类证据时，即使数值范围看似合理，也只能标记为 `observed_unidentified`。

## 脱敏报告

报告包含：

- `schema_version`；
- 集成版本和粗粒度会话时间；
- WSS 基线是否成功；
- 每个步骤的受控标签、轮次、快照结果和已脱敏变化；
- 每个能力的候选路径、数据类型、判定等级和证据步骤；
- 资源上限、超时和无效步骤计数；
- 最终脱敏扫描结果。

报告不包含：

- 设备 ID、Tag、Config Entry ID 或实体唯一 ID；
- JWT、签名材料、WSS 查询参数、手机号或验证码；
- MQTT Client ID、完整 Topic 或原始 Shadow；
- 字符串原文、自由文本步骤名称或未经处理的异常内容。

开始新会话不会立即删除旧报告；只有新报告完成脱敏校验并成功保存后才原子替换。取消或保存
失败时旧报告保持不变。

## 正式实体映射门槛

发现报告不会自动修改 Config Entry 或状态实体。字段进入正式映射前必须同时满足：

1. 两轮实体面板实验结果一致；
2. 有 `reported` 证据；
3. 开启、关闭或档位变化可重复；
4. 用户人工确认业务含义；
5. 路径、类型、无关字段和异常输入均有合成测试。

满足条件后，另行设计和实现版本化的 Q360 只读状态定义，再创建合适的 `binary_sensor`、
`sensor` 或其他实体。发现功能本身不直接生成这些实体。

## 错误处理

对外错误使用固定、无凭据文本：

| 错误码 | 条件 | 是否保留会话 |
|---|---|---|
| `discovery_wss_unavailable` | WSS 未完成订阅 | 否 |
| `discovery_busy` | 已有活动会话 | 是 |
| `discovery_snapshot_timeout` | Shadow `get` 10 秒未返回 | 否 |
| `discovery_invalid_transition` | 操作不符合状态机 | 是 |
| `discovery_step_expired` | 单步骤超过 2 分钟 | 否 |
| `discovery_session_expired` | 会话超过 20 分钟 | 否 |
| `discovery_resource_limit` | 脱敏变化超过上限 | 会话保留，步骤无效 |
| `discovery_report_save_failed` | 脱敏报告保存失败 | 否，旧报告保留 |

错误消息和日志不得附带 Topic、载荷、动态路径、设备标识或异常原文。

## 测试设计

### 单元测试

- 正式照明解析和发现观察同时消费消息且互不影响。
- 发现观察器未启用时不保留任何额外状态。
- 原始设备路径在差分前完成别名化。
- 字符串、疑似时间戳、对象和数组按规则脱敏。
- 两轮一致且恢复基线才产生 `confirmed_candidate`。
- 单轮、冲突、多能力共同变化和未恢复基线均为 `ambiguous`。
- 未经证实的环境数值只能得到 `observed_unidentified`。
- `desired` 不参与确认评分。
- 状态机正常路径和所有非法转换都被覆盖。
- 完成、取消、超时、断线和卸载均释放原始内存与监听器。
- 报告原子替换；保存失败保留旧报告。
- 合成敏感标记在报告、异常和日志中的命中数为零。

### Home Assistant 运行时测试

- 操作按 Config Entry 正确注册和卸载。
- WSS 不可用时开始操作失败关闭。
- Shadow `get`、步骤观察、完成和取消可在真实 HA 事件循环中运行。
- 诊断下载只包含脱敏报告。
- 删除 Config Entry 清除报告和所有发现任务。

### 项目验证门

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy custom_components/aupu_q360
uv run python scripts/check_no_secrets.py
AUPU_RUN_HA_RUNTIME=1 uv run --group ha-test pytest tests/ha_runtime -m ha_runtime -v
```

合成测试通过只能证明程序边界，不能证明浴霸真实字段语义。

## 真实设备验收

1. WSS 通过 Shadow `get/accepted` 建立全关闭基线。
2. 取暖、换气、烘干、摆风、档位和定时按单变量方式各执行两轮。
3. 每轮只改变一个功能，并执行相应关闭步骤恢复基线。
4. 报告不出现真实设备 ID、凭据、手机号、完整 Topic 或原始字符串。
5. 一致字段列为 `confirmed_candidate`，冲突字段不得进入正式映射。
6. 温度、湿度和功率没有独立语义证据时，只报告未识别数值或本次未观察到。
7. 整个会话不得发送除 Shadow `get` 之外的新 MQTT 发布，也不得调用浴霸控制 API。

## Linux Home Assistant 主机开发边界

仓库迁移到 Linux 主机后，开发 checkout 与 HA 运行目录保持分离：

- Git 仓库放在独立开发目录，不直接把仓库根目录作为 HA 的 `/config`。
- 只有通过验证的 `custom_components/aupu_q360` 才同步到
  `/config/custom_components/aupu_q360`；具体同步与回滚命令留给后续实现计划。
- `.private/`、原始 HAR、Cookie、证书和 Windows 本地运行资料不随 Git 仓库迁移。
- 合法凭据只通过 Linux 主机上的 Home Assistant 配置流程录入，不写入 checkout、测试夹具、
  shell history 或提交。
- Linux checkout 使用 `uv sync --locked` 建立开发环境，并原生执行此前 Windows 无法运行的
  HA runtime 测试。
- 开发验证、同步到 HA、重启 HA、真实发现会话和正式字段映射是不同授权阶段；前一步成功
  不自动授权后一步。
- 真实会话前先确认运行中的集成版本与 checkout 提交一致，并备份 HA Config Entry。

## 交付阶段

1. 在 Linux 开发 checkout 实现只读发现基础设施和自动化测试。
2. 通过项目验证门及 Linux HA runtime 测试。
3. 经用户授权后同步到本地 HA，并确认现有照明和状态通道没有回归。
4. 经用户授权启动一次真实发现会话，按实验脚本生成脱敏报告。
5. 用户审查并确认候选字段含义。
6. 对确认字段另行编写只读实体映射设计和实现计划。

提交、推送、Release、HA 部署、HA 重启、真实设备实验和新增正式实体均是独立动作。本设计
的批准只授权规格文档提交，不授权这些后续动作。
