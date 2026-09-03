# Q360 面板状态发现 v2 设计

## 文档状态

- 日期：2026-09-03。
- 状态：设计已确认，尚未实施、部署或启动真实发现。
- 取代范围：本规格取代
  [Q360 只读状态发现设计](./2026-09-02-q360-read-only-state-discovery-design.md)
  中的能力分类、实验状态机、候选分析、报告格式、原始数据保存边界和真实运行流程。
- 运行态边界：当前已部署代码仍是 v1；只有 v2 本地实现和验证通过、同步组件和容器变更分别
  获得授权后，HA 运行态才可切换到 v2。

## 背景

当前集成已经确认照明状态路径，并直接从 AWS IoT Device Shadow 的
`state.reported.<device-id>.2.properties.1` 读取布尔值。`reported` 和 `get_reported`
是设备确认状态，`desired` 或本地命令只形成带 `assumed_state` 的临时状态；WSS 断线时保留
最后值并标记过期。正式照明解析先于可选 discovery observer 执行，因此发现失败不能阻断
照明状态。

已部署的 v1 发现功能复用同一条 WSS 连接，通过只读 Shadow `get` 和面板单变量实验生成
脱敏候选报告。它使用 `heating`、`ventilation`、`drying`、`swing`、`fan_level`、`timer`
等先验标签，并只提供三个档位。真实面板信息证明这些标签和恢复模型不足以表达设备：

- 面板有七个独立开关模式：AI 恒温暖、除臭除菌、换气、吹风、普通干燥、恒温干燥和小夜灯。
- 五个档位是共享的全局选择，没有 `off` 或 `0` 档；所有模式关闭时不能调整。
- 开启换气后可以选择全部五档，因此换气是档位发现的固定载体模式。
- 全局档位保留最后选择值，实验后必须恢复原档位，不能用关闭模式代替恢复档位。
- AI 恒温暖有 `30–42°C`、步进 `1°C` 的设定温度；只有开启 AI 恒温暖后才能调整。
- AI 温度保留最后选择值，实验只调整相邻 `1°C` 并恢复原温度。

现有照明路径已经解密。小夜灯仍作为真实面板模式参与只读实验，用于确认它与现有照明路径
是同一状态、独立状态还是共享编码；本规格不因此创建重复照明实体。

## 核心原则

实验状态机只管理操作顺序和 Action 合法性，不推导设备状态。设备真实状态的唯一来源是目标
设备 Shadow 返回的 `reported`：

```text
实验状态机 ──> 告诉用户下一步在面板做什么
Shadow reported ──> 提供步骤前后和恢复后的真实设备状态
```

状态机进入“等待开启”或“等待恢复”只表示下一项预期操作，不能生成 `on`、档位或温度值。
用户声明的操作、`desired`、本地命令结果和状态机阶段均不能作为设备确认。每个阶段必须通过
关联 Shadow `get/accepted` 的完整 `reported` 快照结束，并保留阶段期间收到的
`update/accepted` 中的 `reported` 变化。

发现阶段尚不知道七种模式对应的 service/property，只能将受控实验标签与实际返回差异建立
证据关系。字段和编码经真实报告确认后，正式实体应在后续独立规格中仿照照明直接解析
`reported`，不得依赖实验状态机。

## 目标

1. 用真实面板分类替换 v1 的假设分类，并准确表达七种模式、五档和 AI 设定温度。
2. 每次发现先续建唯一认证 WSS，再复用 MQTT 解码和 Shadow `get` 观察完整目标设备 `reported`。
3. 通过两轮一致、方向可逆的面板单变量实验，将业务标签与 service/property 路径及值编码关联。
4. 区分实验流程阶段与真实设备状态，所有候选和恢复结论只使用 `reported` 证据。
5. 在 Linux 主机的 Git 和 HA `/config` 之外保存可校验的本机原始发现档案，供后续离线复核。
6. 在 HA Diagnostics 中继续只提供脱敏报告，避免原始数据进入常规导出和上传路径。
7. 保持现有照明、WSS 重连、状态通道和认证行为不变。

## 非目标

- 不在本规格中创建七种模式、五档或温度的正式 HA 实体。
- 不发送这些模式、档位或温度的控制命令，不写 Shadow `desired` 或 `update`。
- 不根据状态机阶段、字段编号、数值范围、名称相似性或单次变化猜测业务含义。
- 不把 `desired`、HTTP 控制成功或用户口头确认当作设备执行证据。
- 不修改现有照明控制协议和已确认的照明状态路径。
- 不新增第二条 WSS/MQTT 连接，不探测局域网协议，不导入 HAR、PCAP 或小程序登录材料。
- 不采集 WSS 握手参数、JWT、Cookie、Authorization、签名密钥、短信验证码或登录请求。
- 不把原始发现档案放入 Git、HA `/config`、HA Diagnostics、日志、通知、Issue 或聊天。
- 不自动上传、共享、清理或解释原始档案。

## 总体架构

```text
现有认证 WSS / MQTT 解码
          │
          ├─ accepted Shadow 解析 ─> 正式照明 reported 解析 ─> HA light
          │                                │
          │                                └─ 始终先执行，发现失败不能影响它
          │
          └─ 活动发现 observer
                 ├─ 原始事件队列 ─> Linux 私有原始档案
                 └─ reported 快照 ─> 阶段差异 ─> v2 脱敏报告
```

集成继续只解码一次已接受的目标 Shadow 消息。传输层保留原始 topic 和 payload 字节，并与解析
后的 `AcceptedShadow` 一起放入不可回显的内部事件；协调器先应用正式照明状态，再将事件交给
活动发现 observer。没有发现会话时不创建原始档案，也不保留额外 payload。

发现发送的每个 Shadow `get` 使用进程内随机 client token。只有 topic、token 和目标设备根均
匹配的完整 `get/accepted` 才能结束快照等待。阶段期间的 `update/accepted` 用于保存瞬时变化，
但不能替代阶段结束所需的完整 `get/accepted` 快照。

### 组件边界

#### 1. 固定实验目录

实验目录是能力、参数范围、载体模式和合法阶段的唯一事实来源。Action schema、状态机、分析器、
翻译和测试从同一目录取值，不能分别维护不一致的枚举。

#### 2. 发现会话

发现会话负责关联快照、管理阶段、收集 `reported` 变化、限制资源和清理内存。它不解释正式
业务状态，也不接触文件路径之外的系统配置。

#### 3. 原始档案写入器

原始档案写入器只接受有界的目标 Shadow 事件和受控阶段元数据。文件 I/O 通过有界异步队列与
WSS 回调隔离；队列溢出或写入失败会取消发现，但不能阻塞或回滚已应用的正式照明状态。

#### 4. 候选分析器

候选分析器只消费阶段化 `reported` 差异。它判断重复性、可逆性、共享路径和覆盖率，不读取
认证材料，也不向设备发送消息。

#### 5. 脱敏报告存储

HA Store 继续以 Config Entry 为作用域原子保存最近一次 v2 脱敏报告。原始档案有独立生命周期，
删除 Config Entry 不自动删除主机档案。

## 固定实验分类

### 模式实验

| 内部标识 | 面板名称 | 操作目标 | 恢复目标 |
|---|---|---|---|
| `ai_thermostatic_warmth` | AI 恒温暖 | `on` | `off` |
| `deodorization_sterilization` | 除臭除菌 | `on` | `off` |
| `ventilation` | 换气 | `on` | `off` |
| `air_blowing` | 吹风 | `on` | `off` |
| `normal_drying` | 普通干燥 | `on` | `off` |
| `thermostatic_drying` | 恒温干燥 | `on` | `off` |
| `night_light` | 小夜灯 | `on` | `off` |

模式之间不假定互斥。为了获得可解释证据，每轮模式实验开始时由用户确认其他六种模式保持
关闭；系统不因为该确认而生成状态，只记录随后返回的 `reported`。

### 参数实验

| 内部标识 | 固定载体 | 合法原值与目标值 | 约束 |
|---|---|---|---|
| `global_fan_level` | `ventilation` | `level_1` 至 `level_5` | 原值与目标值不同 |
| `ai_target_temperature` | `ai_thermostatic_warmth` | 整数 `30` 至 `42` | 原值和目标值相差 `1` |

载体由实验目录推导，Action 不能提交或覆盖载体。档位没有 `off` 或 `0`。同一会话内所有档位
实验使用同一个原档位，每次切换目标档位后恢复原档位。若原档位为三档，测试
`3↔1`、`3↔2`、`3↔4`、`3↔5` 即覆盖五个标签，原档位通过反向恢复证据参与映射。

温度实验在两轮中使用相同的原温度和相邻目标温度。原温度为 `30` 时只能测试 `31`，原温度
为 `42` 时只能测试 `41`；其他温度可选择任一相邻整数，但两轮方向必须一致。

### 空闲实验

`idle_environment` 在所有模式关闭时观察，不操作面板。它用于识别持续变化、环境或设备健康
字段，并帮助将非业务波动与模式、档位、温度候选分开。空闲变化永不单独成为已确认控制候选。

## Home Assistant Action 接口

v2 注册以下 Config Entry 定向 Action：

- `aupu_q360.start_discovery`
- `aupu_q360.begin_discovery_step`
- `aupu_q360.advance_discovery_step`
- `aupu_q360.finish_discovery`
- `aupu_q360.cancel_discovery`

v1 的 `complete_discovery_step` 被 `advance_discovery_step` 取代，不保留同时表达两套状态机的
兼容入口。

`start_discovery` 必须包含 `config_entry_id` 和值为 `true` 的“面板所有模式已关闭”确认。该确认
是操作前置条件，不是设备状态证据。Config Entry 已显式启用原始档案时，启动还必须检查固定
私有挂载存在、不是符号链接、目录权限正确且可写。

`begin_discovery_step` 必须包含 `config_entry_id`、固定 `experiment` 和 `round`，轮次只能为
`1` 或 `2`。字段矩阵如下：

- 模式：不得提交参数原值或目标值。
- 全局档位：必须提交不同的 `source_level` 和 `target_level`，两者均为 `1–5` 的整数。
- AI 温度：必须提交 `source_temperature` 和 `target_temperature`，均为 `30–42` 的整数且相差
  `1`。
- 空闲：不得提交参数原值或目标值。

`advance_discovery_step` 只包含 `config_entry_id`。调用方不能提交阶段；系统根据当前合法流程
发送一次 Shadow `get`、取得真实 `reported`、保存阶段证据，再返回下一个固定提示码。

`finish_discovery` 只在没有活动步骤和待恢复阶段时可执行。允许保存覆盖不完整的报告，但必须
在 `coverage` 中逐项区分 `not_started`、`partial` 和 `complete`；未开始实验不能伪装成
`not_observed`。

`cancel_discovery` 终止软件采集，不发送设备恢复命令。若已有面板变化尚未获得恢复证据，响应
必须包含固定的人工检查提示码。

所有 Action 响应只包含固定状态、提示码、阶段、计数和布尔值，不回显 raw payload、真实路径、
设备标识、client token、文件绝对路径或异常详情。

## 实验状态机

### 会话状态

```text
IDLE
  └─ start ─> ARCHIVE_OPENING ─> SESSION_BASELINING ─> READY

READY
  ├─ begin ─> STEP_BASELINING ─> AWAITING_OPERATOR
  ├─ finish ─> FINALIZING ─> IDLE
  └─ cancel ─> CANCELLED ─> IDLE

AWAITING_OPERATOR
  ├─ advance + valid reported ─> 下一实验阶段或 READY
  ├─ no restoration evidence ─> RESTORE_REQUIRED
  └─ timeout/disconnect/cancel ─> CANCELLED ─> IDLE

RESTORE_REQUIRED
  ├─ advance + restoration evidence ─> 下一恢复阶段或 READY
  └─ timeout/disconnect/cancel ─> CANCELLED ─> IDLE
```

状态名只表达软件流程。`AWAITING_OPERATOR` 不包含设备当前开关、档位或温度结论。

### 开始会话

启动顺序固定为：

1. 验证 Config Entry 已加载、未处于 Reauth/停止状态、WSS 配置完整且无其他发现会话。
2. 完整停止旧 runner，启动唯一的新 WSS runner，并在 45 秒内等待订阅完成后的首个 PINGRESP。
3. 若启用原始档案，创建权限受限的会话目录和 `.partial` 文件并启动写入任务。
4. 挂载 observer，使基线请求和响应都进入本次档案。
5. 启动完整会话 timer，并发送带随机 token 的 Shadow `get`。
6. 在 10 秒内取得关联的完整目标 `reported`，作为“用户声明所有模式关闭”的会话基线。

基线是实际返回快照，但在映射未知时不能仅凭内容证明面板确实全关；因此需要用户确认和后续
可逆实验共同约束。

### 模式周期

每种模式执行两个完整周期：

```text
begin ─> 获取步骤前 reported
      ─> 提示用户开启该模式
advance ─> 获取开启后 reported，记录 mode_on
        ─> 提示用户关闭该模式
advance ─> 获取关闭后 reported，记录 mode_restore
        ─> 路径级恢复证据成立后回到 READY
```

系统不能因为用户调用第二次 `advance` 就判定模式已关闭。模式候选必须在 `mode_on` 中出现
可重复变化，并在 `mode_restore` 中回到该路径的步骤前值。

### 参数周期

档位和温度使用相同的四阶段结构：

```text
begin ─> 获取会话关闭状态下的步骤前 reported
      ─> 提示用户开启固定载体
advance ─> 获取 carrier_on reported，形成载体局部基线
        ─> 提示用户从原值切换到目标值
advance ─> 获取 parameter_change reported
        ─> 提示用户恢复原值
advance ─> 获取 parameter_restore reported
        ─> 提示用户关闭载体
advance ─> 获取 carrier_off reported，完成周期
```

参数候选只能来自 `parameter_change`，并且同一路径必须在 `parameter_restore` 回到载体局部
基线值。载体候选来自 `carrier_on`，并在 `carrier_off` 回到步骤前值。载体阶段的变化不能归因
给参数，参数阶段的变化也不能替代载体恢复证据。

### 空闲周期

每轮先获取步骤前 `reported`，提示用户保持面板不变并等待 15–30 秒，再由 `advance` 获取完整
`reported`。期间变化只进入 `idle_observation`。

### 路径级恢复判定

完整 Shadow 可能包含时钟、环境或健康字段，不能要求整个 JSON 与早期快照逐字节相等。恢复
判定以正向阶段实际变化的路径集合为范围：

- 模式恢复比较 `mode_on` 中变化路径的步骤前值与 `mode_restore` 结束值。
- 参数恢复比较 `parameter_change` 中变化路径的载体局部基线值与
  `parameter_restore` 结束值。
- 载体关闭比较 `carrier_on` 中变化路径的步骤前值与 `carrier_off` 结束值。
- 时间戳和只在空闲实验中持续变化的路径记录为背景波动，不阻塞操作流程。

首次实验尚无已确认路径时，只要存在至少一个非背景路径形成正向变化和反向恢复，流程可以
回到 `READY`；未恢复路径保留为模糊证据。正向阶段完全没有非背景变化时记录未观察证据，
继续完成面板恢复提示，不进入 `RESTORE_REQUIRED`。正向阶段存在非背景变化但没有任何路径
形成恢复证据时，才进入 `RESTORE_REQUIRED`。已有同实验已确认路径时，该路径未恢复必定进入
`RESTORE_REQUIRED`。

此规则只判断返回值是否恢复，不宣称系统已经知道字段业务名称。用户仍需在面板视觉确认模式、
档位和温度已经恢复。

## 原始发现档案

### 保存位置与挂载

主机固定根目录：

```text
/home/george/.local/state/ha-aupu-q360/raw-discovery/
```

容器固定挂载点：

```text
/var/lib/aupu-q360-private-discovery/
```

部署时使用独立读写 bind mount。原始数据不写入 HA `/config`，从而不进入 HA 配置备份；也不
位于 Git 仓库。主机根目录和每个会话目录权限为 `0700`，文件权限为 `0600`。代码拒绝符号
链接、路径逃逸、已存在的冲突会话目录和任意用户提供的保存路径。

原始档案是 Config Entry 的显式布尔选项，默认关闭。选项只控制是否使用固定挂载，不允许填写
路径。当前真实 Task 9 运行计划启用此选项；启用后挂载不可用即拒绝开始，不能静默退化为只
保存脱敏报告。

### 保存内容

每条档案事件包含：

- 单调递增序号；
- UTC 接收或发送时间；
- 受控实验、轮次和阶段；
- `incoming` 或 `outgoing` 方向；
- 完整目标 Shadow MQTT topic；
- 原始 MQTT payload 字节的 Base64 表示。

Base64 只用于无损表示字节，不是脱敏。原始 topic、设备 ID、service/property、JSON 值、
Shadow version、timestamp 和 client token 均可存在于私有档案。发现只归档本会话发送的
Shadow `get` 及收到的目标 `get/accepted`、`update/accepted`；不归档 WSS URL、握手查询参数、
MQTT CONNECT、认证 HTTP 请求或任何凭据。

### 文件结构与完成语义

每个会话使用不可预测的本地会话 ID 创建目录，并包含：

- `events.jsonl.partial`：活动或不完整事件档案；
- `events.jsonl`：成功关闭并校验后的事件档案；
- `manifest.json`：会话状态、事件数、文件字节数和 `events.jsonl` SHA-256。

写入器使用 `O_EXCL`、拒绝跟随符号链接，并在完成时刷新、关闭、计算 SHA-256，再以原子重命名
将 `.partial` 变为正式文件。manifest 也通过临时文件原子替换。失败、取消、断线或 HA 停止时
保留 `.partial`，manifest 标记 `incomplete`；不得为不完整文件伪造成功哈希。

单次 `events.jsonl` 编码后上限为 64 MiB，单个 MQTT 包继续受 65,536 字节上限约束。达到限制、
队列溢出或文件系统错误会停止会话并保留已有数据。档案不自动轮换或删除；清理必须是后续明确
授权的本机操作。

## 阶段差异与值处理

发现会话在内存中保留完整原始值以进行同会话比较，并同时生成用于 HA 报告的安全表示：

- 布尔值直接保存。
- `-1000` 至 `1000` 范围内的有限数值可直接保存；范围外数值使用会话级 HMAC 指纹。时间戳
  只保存精度和相对变化。
- 字符串和大数值保存会话级 HMAC 指纹及长度或类型信息。
- 数组和对象先进行有界、稳定的 JSON 规范化，再保存会话级 HMAC 指纹、深度和节点数。

数组或对象不能只按结构大小比较；内容变化但形状不变时，规范化 HMAC 必须产生不同指纹。
会话 HMAC 密钥只存在于内存，完成或取消时清除。原始档案不使用这些脱敏规则。

每个阶段的差异包含路径、类型、方向、前后安全表示和瞬时变化次数。路径只保留目标设备下的
`service/<decimal>/property/<decimal>`，不包含 device ID。

## 候选分析

### 分类

- `confirmed_candidate`：要求的两轮均完成，路径和类型一致，正向变化签名可重复，反向恢复
  成立，且没有无解释冲突。
- `ambiguous`：存在变化，但轮次不一致、方向不稳定、恢复证据不足或多个解释无法区分。
- `observed_unidentified`：只在空闲观察中出现，不能关联模式或参数。
- `not_observed`：实验按要求完成两轮，但没有观察到可归因变化。
- `invalid`：相关实验发生超时、资源上限、非法 payload 或档案失败，证据不可用于确认。

未运行的实验不产生 `not_observed` 候选，由 `coverage=not_started` 表达；只完成部分轮次则为
`coverage=partial`。

### 专用路径与共享路径

同一路径被多个模式改变不再自动判为模糊。若每种模式在两轮中都有独立、可重复、可逆且彼此
可区分的值签名，该路径可成为 `confirmed_candidate`，并标记 `association=shared`。典型情况是
共享枚举或位掩码。

只关联一个实验的路径标记 `association=dedicated`。多个实验在同一路径产生相同、不可重复或
不可逆签名时仍为 `ambiguous`。时间戳、心跳计数和空闲持续变化不得用于区分共享模式。

### 参数归因

全局档位路径必须在 `parameter_change` 中从声明原档位对应值变为目标档位对应值，并在
`parameter_restore` 中反向恢复。两轮签名必须一致。原档位通过每轮载体局部基线和恢复值参与
映射，因此不要求执行“原档位切换到自身”的无变化步骤。

AI 温度路径必须在两轮相同的相邻 `1°C` 操作中稳定增减，并在恢复阶段回到原值。看似落在
`30–42` 范围但没有阶段相关和可逆证据的数字不能确认成温度。

### 状态来源约束

候选只使用 `reported`。包含同一路径的 `desired` 可以保存在原始档案中，但不得参与候选、恢复、
覆盖率或设备确认时间。发现 observer 的异常不能阻止协调器先应用已知照明 `reported`。

## v2 脱敏报告

报告使用不兼容的 `schema_version: 2`，包含：

- 集成版本和按小时截断的会话开始时间；
- WSS 基线和原始档案状态；
- 每个实验的 `not_started`、`partial` 或 `complete` 覆盖率；
- 每个周期的受控标签、轮次、阶段结果和路径级恢复布尔值；
- 脱敏阶段差异和候选分类；
- 原始档案会话 ID、完成状态、事件数、文件字节数和 SHA-256；
- 快照、阶段、会话、变化数、包大小和档案大小限制；
- 完成周期、无效周期、超时和恢复失败计数；
- 最终安全扫描结果。

报告不包含原始 payload、完整 topic、device ID、client token、文件绝对路径、JWT、手机号、
Authorization、签名材料或自由文本。原始档案会话 ID 只用于本机定位，不编码设备 ID 或时间。
原始档案未启用时，报告只保存 `enabled=false` 和 `status=not_requested`，不得生成会话 ID、
事件数、字节数或 SHA-256 伪造值。

`confirmed_candidate` 只证明字段与面板操作稳定相关，不证明该字段可以安全写入，也不触发实体
创建。报告经固定 schema 验证和敏感值扫描后才原子替换旧报告。报告失败保留旧报告；已成功
完成的原始档案不删除。

## 错误处理与清理

错误对外只使用固定代码，日志不附带异常文本或 payload。v2 保留现有 WSS、快照、步骤、会话、
资源和报告错误，并增加：

- `discovery_raw_archive_unavailable`
- `discovery_raw_archive_failed`
- `discovery_raw_archive_limit`
- `discovery_invalid_parameter`
- `discovery_restore_required`
- `discovery_manual_restore_required`

处理规则：

1. 私有挂载不存在、权限不符或不可写：在发送基线 `get` 前拒绝启动。
2. 原始档案写入失败、队列溢出或达到上限：停止会话，保留 `.partial` 和旧报告。
3. 恢复证据不足：进入 `RESTORE_REQUIRED`，禁止开始下一实验，允许用户修正后再次
   `advance`。
4. 断线、鉴权失败、阶段超时、会话超时、卸载、HA 停止或取消：停止 observer 和 timer，清除
   内存快照、HMAC 密钥和 token；若面板已变化但未确认恢复，返回人工检查提示。
5. 档案完成失败：保留 `.partial`，不生成声称拥有有效档案哈希的新报告。
6. 报告验证或保存失败：保留旧报告和已经完成的原始档案。
7. 删除 Config Entry：删除对应 HA 脱敏报告，不删除主机原始档案。

软件无法只读地强制设备恢复。任何失败提示都必须明确要求用户检查模式、档位和温度，不能声称
已经自动关闭取暖或其他模式。

## 资源限制

- 快照等待：10 秒。
- WSS 续建与健康确认：45 秒；不进入报告 limits。
- 每个等待用户操作的阶段：300 秒。
- 整个发现会话：3,300 秒。
- 每阶段脱敏变化：256 条。
- 单个 MQTT 包：65,536 字节。
- 单次原始 JSONL 档案：64 MiB。
- 活动发现会话：每个 Config Entry 一个；同一协调器只允许一个 discovery observer。

阶段超时按每次用户操作重新计时；会话总时限不会因阶段推进而重置。
新报告写入完整 `300/3,300` profile；验证器仍接受旧 v2 报告的完整 `120/3,600` profile，但拒绝
新旧阶段与会话时限混搭。

## 与现有照明行为的兼容性

现有照明继续严格读取 `reported.<device-id>.2.properties.1` 布尔值：

- 同一消息同时含 `reported` 和 `desired` 时，`reported` 优先。
- `reported` 和 `get_reported` 清除 `assumed_state` 和 `state_stale`。
- HA 照明命令只产生临时 `source=command`，直到设备返回 `reported`。
- 只有 `desired` 时状态保持未确认。
- WSS 断线保留最后值并标记过期，重连后通过 Shadow `get` 刷新。
- 正式照明解析在 discovery observer 前执行，observer 或原始档案失败不能回滚照明状态。

v2 不新增照明字段推导。小夜灯实验若与现有照明路径相关，只在报告中记录共享或专用证据；
是否复用现有实体由真实报告后的下一份规格决定。

## 测试设计

所有自动化测试只使用合成设备 ID、token、topic 和 Shadow；真实原始档案、Config Entry 备份、
HAR、PCAP、Cookie 和凭据不得复制进仓库或测试夹具。

### 单元测试

- 固定目录精确包含七种模式、五档、温度范围、载体和空闲实验。
- 拒绝档位相同、档位越界、温度越界、非整数温度、非相邻温度、错误轮次和无关字段。
- 模式、参数、空闲的每个合法状态转换和所有非法转换。
- 状态机阶段不生成设备状态；缺少 `reported` 时只能超时或产生未观察证据。
- `reported` 优先于 `desired`，`desired` 不参与恢复或候选。
- 模式、参数和载体均以实际返回路径进行正向和反向比较。
- 时间戳和空闲波动不导致整份快照恢复失败。
- 独立布尔、共享枚举、位掩码、字符串、数组和对象字段的两轮一致性。
- 数组或对象内容改变但结构不变时，规范化 HMAC 能检测差异。
- `dedicated`、`shared`、`ambiguous`、`observed_unidentified`、`not_observed` 和 `invalid`
  的稳定排序与覆盖率。

### 原始档案测试

- 固定根目录、拒绝符号链接和路径逃逸、目录 `0700`、文件 `0600`。
- topic 和 payload 字节经 Base64 往返后完全一致，事件顺序和阶段元数据稳定。
- 写入完成、SHA-256、原子重命名、manifest 原子保存和成功状态一致。
- 取消、断线、写入错误、队列溢出和大小上限保留 `.partial` 并标记不完整。
- 使用可注入的小限制模拟 64 MiB 边界，不在测试中创建巨大文件。
- 档案只收到发现期目标 Shadow get/get-accepted/update-accepted，不收到握手或认证数据。
- 原始内容不进入 repr、日志、Action 响应、HA Store 或 Diagnostics。

### Home Assistant runtime 测试

- 五个 v2 Action 的注册、Config Entry 定向、selector 和响应字段。
- start 先关闭旧 WSS 并等待新连接 healthy；启用原始档案但固定挂载不可用时，在相关发现
  Shadow `get` 前失败。
- 使用真实 HA service registry 和 fake WSS 完成模式、档位、温度和空闲合成会话。
- 正式照明先于发现 observer 更新；observer、分析器或档案写入失败不能阻止照明 `reported`。
- WSS 断线只将最后照明状态标记过期，不能反转或清空。
- Config Entry 卸载、HA stop、多个 Config Entry 和服务卸载没有 listener、timer、writer task 泄漏。
- Diagnostics 只包含 v2 脱敏报告和原始档案元数据，不包含原始 topic/payload 或绝对路径。

### 网络守卫与静态验证

- discovery 代码路径只能发布 Shadow `get`，不得发布 Shadow `update`、`desired` 或调用设备控制
  HTTP 接口。
- 原始档案写入器没有网络能力。
- 运行项目全部 pytest、HA runtime 测试、网络守卫、Ruff、格式检查、mypy、秘密扫描和
  `git diff --check`。

## Linux 开发、部署与真实验收边界

开发仓库继续位于 `/home/george/projects/python/ha-aupu-q360`，HA `/config` 与原始档案根目录均
与 Git 分离。实现阶段只在 Linux 仓库使用项目 `uv` 环境和合成数据；不读取当前 Config Entry
备份内容，不连接真实浴霸，不把私有材料迁入仓库。

本地验证通过后，运行态变更分为独立步骤：

1. 获得授权后创建主机私有根目录并核对 `0700` 权限。
2. 获得授权后备份并修改 HA Compose，增加固定 bind mount；先运行 Compose 配置解析。
3. 获得授权后同步自定义组件的精确允许文件，并保留可恢复版本和哈希清单。
4. 获得授权后重新创建或重启 HA 容器，验证 HTTP、版本、集成、照明和 connectivity 实体。
5. 部署不会自动启用原始档案选项或启动 discovery。
6. 真实实验开始前再次核对用户在面板旁、所有模式关闭、原档位和原温度可见。

真实验收由用户操作实体面板，HA 只发送 Shadow `get`。完整建议顺序为：

1. 两轮空闲观察。
2. 七种模式各完成两轮开启和关闭周期；每次只改变一个模式。
3. 以当前保留档位为原值，对其他四档各完成两轮换气载体周期。
4. 以当前 AI 温度为原值，对同一相邻温度完成两轮 AI 恒温暖载体周期。
5. 完成报告，核对原始档案权限、完成状态、事件数、字节数和 SHA-256，但不输出内容。
6. 审计会话期 MQTT 发送记录，确认 discovery 只有 Shadow `get`。
7. 人工检查所有模式关闭，档位和温度恢复到实验前选择。

真实报告可能证明某些模式共享一个枚举、位掩码或对象，也可能没有返回可用字段。只有实际
`reported` 证据决定结论。发现结束后另写实体和控制规格；本规格不预先承诺实体类型或写入编码。

## 完成标准

本规格的实现只有同时满足以下条件才算完成：

1. 固定实验目录、状态机、原始档案、分析器、v2 报告和 Action 全部实现。
2. 所有自动化验证在 Linux 原生 HA runtime 环境通过，且测试夹具全部为合成数据。
3. discovery 网络守卫证明没有模式、档位、温度或 Shadow update 控制路径。
4. 原始档案位于 Git 和 HA `/config` 之外，权限、大小上限、失败保留和哈希语义通过测试。
5. HA Diagnostics、日志、Action 响应和 Git 中不存在原始数据或凭据。
6. 现有照明状态来源、乐观状态、断线过期和 WSS 重连测试无回归。
7. 文档明确区分本地实现、运行态部署和真实面板实验的独立授权边界。

代码实现和部署成功仍不等于字段映射成功。只有用户完成真实面板实验并审查实际 `reported`
证据后，才能决定后续正式实体设计。
