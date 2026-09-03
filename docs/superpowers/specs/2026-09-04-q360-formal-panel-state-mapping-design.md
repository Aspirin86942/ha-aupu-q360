# Q360 正式面板状态映射设计

## 文档状态

- 日期：2026-09-04。
- 状态：方案 A 与全部设计段已确认。
- 当前基线：`main` 的 `0.2.4`，提交 `5b638fc`。
- 适用范围：本仓库的 Q360 Shadow 解析、Coordinator、HA 实体、Diagnostics、临时探针清理、
  测试、用户文档、版本和本机 HA 部署。
- 前置设计：
  `docs/superpowers/specs/2026-09-03-q360-discovery-ablation-design.md`。
- 关系：本文用真实 `reported` 证据完成前置设计约定的正式映射，并永久删除一次性探针。

## 背景与结论

2026-09-03 至 2026-09-04 的自适应手机实验通过现有唯一 AWS IoT MQTT-over-WSS 连接取得了
24 份相邻样本。实验只由用户在奥普官方 App 操作设备；HA 端只发关联 Shadow `get`，没有发送
Shadow `update`、写入 `desired` 或调用设备控制接口。

实验确认六种互斥运行模式共用一个整数属性，另有小夜灯布尔状态、全局风量整数和 AI 目标
温度整数。该结构不支持把六种运行模式描述成六个独立原始布尔字段，因此采用一个枚举 sensor
表达当前运行模式。小夜灯、风量和 AI 目标温度分别用只读实体表达。

正式集成只消费目标设备的 `state.reported`。它不增加任何模式、档位或温度控制能力，不把
持续递减但语义尚未完全确认的字段发布成“剩余时间”，也不保留临时探针作为调试后门。

## 真实证据与映射范围

每项正式映射都至少有一次单变量变化和一次恢复；表中只记录规范化路径和值，不包含设备 ID、
topic、token 或原始 payload。

### 当前运行模式

路径：`service/3/property/2`。

| reported 整数 | 正式状态键 | 中文显示 | 实验证据 |
|---:|---|---|---|
| `0` | `off` | 关闭 | 每种模式关闭后均恢复为 `0` |
| `18` | `ai_thermostatic_warmth` | AI 恒温暖 | 开启 `18`，关闭 `0` |
| `21` | `deodorization_sterilization` | 除臭除菌 | 开启 `21`，关闭 `0` |
| `7` | `ventilation` | 换气 | 开启 `7`，关闭 `0` |
| `2` | `air_blowing` | 吹风 | 开启 `2`，关闭 `0` |
| `9` | `normal_drying` | 普通干燥 | 开启 `9`，关闭 `0` |
| `4` | `thermostatic_drying` | 恒温干燥 | 开启 `4`，关闭 `0` |

这里的模式是互斥枚举。任何未列出的整数只映射为固定状态 `unknown`，不得猜测名称、保留旧
模式或在日志/Diagnostics 中暴露原始整数。

### 小夜灯

路径：`service/6/property/4`。

- 手工开启：`false -> true`。
- 静置复核：保持 `true`。
- 手工关闭：`true -> false`。

该实体表达设备 Shadow/官方 App 报告的小夜灯逻辑状态。部分模式操作会让官方 App 中的开关
短暂显示开启并随后自动关闭；正式实体忠实显示 `reported`，不推断物理发光、自动化来源或
关闭原因。

### 风量档位

路径：`service/6/property/5`。

- 原档位 `5` 改为 `4`：`5 -> 4`。
- 恢复原档位：`4 -> 5`。

正式值域为整数 `1..5`，显示单位为“档”。当前设计只读，不创建 number、select 或 fan 控制
实体。

### AI 目标温度

路径：`service/3/property/3`。

- 原目标温度 `36℃` 改为 `34℃`：`36 -> 34`。
- 恢复原目标温度：`34 -> 36`。

正式值域为整数 `30..42`，单位为 `℃`。它是目标设定值而非环境测量值，不声明长期统计用的
measurement state class，也不创建 climate 或 number 控制实体。

## 明确排除的候选

| 路径 | 观察 | 结论 |
|---|---|---|
| `service/6/property/1` | 模式开启后常为 `30`、`60`、`90` 或 `120`，并随时间递减 | 名称、单位和全部语义未充分确认，本轮不建实体 |
| `service/4/property/1` | 空闲时也在约 `30..41` 间漂移 | 背景变化，不能映射为本次操作字段 |
| `service/6/property/23` | App 操作后短暂 `0 -> 1 -> 0` | 瞬态信号，不代表持续模式 |

任何未来映射都需要新的真实证据和独立设计；不得把这些排除项留成隐藏属性、Diagnostics 字段
或未启用代码。

## 采用方案与未采用方案

### 采用：一个模式枚举和三个独立只读状态

正式实体为：

1. 当前运行模式 enum sensor；
2. 小夜灯 binary sensor；
3. 风量档位数值 sensor；
4. AI 目标温度 sensor。

这与真实 Shadow 模型一致，实体数量最少，且不会暗示未经确认的控制能力。

### 未采用：六个派生模式 binary sensor

六个实体都将由同一个整数派生。它们会隐藏互斥关系、扩大实体和自动化表面，并让用户误以为
设备报告了六个独立开关。

### 未采用：枚举和六个派生 binary sensor 同时提供

混合方案重复同一事实，增加实体注册、翻译、迁移和长期兼容成本，没有新增可信信息。

## 架构与数据流

```text
奥普云 Shadow get/update accepted
              │
              v
      parse_accepted_shadow
              │
              v
    目标设备 AcceptedShadow
              │
              ├─> parse_light_shadow_update
              │          └─> 现有正式照明状态
              │
              └─> parse_panel_shadow_update
                         └─> reported-only PanelStateUpdate
                                      │
                                      v
                              AupuCoordinator
                                      │
                         一次消息一次 listener 通知
                                      │
                       ┌──────────────┼──────────────┐
                       v              v              v
                  mode sensor   numeric sensors  night-light
                                                 binary sensor
```

`AupuShadowWebSocket` 仍保持唯一连接、现有订阅、keepalive、重连和建连后的空 `{}` Shadow
`get`。这个初始 `get` 用于 HA 启动或 WSS 重连后取得完整 `reported`，不是临时探针接口。

`AupuCoordinator.async_apply_shadow_message()` 固定先处理正式照明，再处理面板状态。解析和应用
同一条消息后最多向普通实体 listener 扇出一次通知，避免四个新增实体和照明实体读取互相不
一致的中间状态。命令、连接和认证状态变化仍可独立通知。

## Shadow 解析模型

### 输入边界

- 只接受目标 thing 的 `shadow/get/accepted` 与 `shadow/update/accepted`。
- 面板解析只读取 `state.reported[device_id]`；对应 `desired` 一律忽略。
- 只访问四个已确认路径，不遍历、不保存也不返回其他 service/property。
- Python `bool` 必须与 `int` 严格区分；不做字符串、浮点数或 `null` 转换。

### 部分更新

Shadow `update/accepted` 可能只包含部分属性。解析器使用明确的字段更新模型区分：

- 路径缺失：`present=false`，Coordinator 保留该字段上一次确认值；
- 路径存在且值有效：`present=true`，应用规范化值；
- 路径存在但类型或范围无效：`present=true, value=null`，仅清空受影响字段。

建议的类型边界为泛型不可变 `PanelFieldUpdate[T]` 和包含四个字段的
`PanelStateUpdate`。如果四个路径全部缺失，解析器返回 `None`。

### 值处理

| 字段 | 有效输入 | 正式输出 | 其他输入 |
|---|---|---|---|
| 当前模式 | 排除 `bool` 后的精确整数 | 七个已知状态之一；其他整数为 `unknown` | 非整数为不可用 |
| 小夜灯 | 精确 `bool` | `true` / `false` | 不可用 |
| 风量 | 排除 `bool` 后的精确整数 `1..5` | 原整数 | 不可用 |
| AI 目标温度 | 排除 `bool` 后的精确整数 `30..42` | 原整数 | 不可用 |

错误输入不得进入异常文本、对象 `repr`、日志、Diagnostics 或实体属性。同一消息中的一个字段
无效不能阻止其他合法字段更新，也不能撤销已经处理的正式照明状态。

探针删除后，`AcceptedShadow.client_token` 不再有消费者，应与 token 长度校验一起删除。顶层
JSON/Shadow 结构和目标 topic 校验继续由现有 `parse_accepted_shadow()` 负责。

## Coordinator 状态

Coordinator 为四个正式字段各保存一个规范化值和一个“已由当前 WSS 连接确认”的 freshness
标记，初始值均为 `None`、freshness 均为 `false`：

- `panel_mode: PanelMode | None`；
- `night_light_is_on: bool | None`；
- `fan_level: int | None`；
- `ai_target_temperature: int | None`。

只有 `present=true` 的字段能替换现值。有效值替换为规范化状态并把该字段 freshness 设为
`true`；无效值替换为 `None`，但不能影响其他字段。缺失字段不得被清空或错误标记为已确认。
WSS 断线把四个 freshness 标记全部设为 `false`，但保留最后的规范化值用于内部诊断。

新增实体的可用性由两个条件共同决定：

1. 当前 WSS 已连接；
2. 对应字段已经由当前连接确认；
3. 对应 Coordinator 值不是 `None`。

断线时保留内存中的最后值，但实体立即不可用；重连后不会因为 transport 先变成 connected 而
短暂展示旧值，必须等初始 Shadow `get` 重新提供对应有效字段。未知但类型正确的模式整数规范化
为 `unknown`，因此该模式字段一经当前连接确认，实体仍可用并明确显示“未知”。

## Home Assistant 实体

所有新增实体都归属现有 `(aupu_q360, config_entry_id)` 设备，不在 unique ID、名称或属性中使用
真实 device ID 或 tag。

| 平台 | translation key | unique ID 后缀 | 状态 |
|---|---|---|---|
| `sensor` | `current_mode` | `_current_mode` | enum：`off`、六种模式、`unknown` |
| `binary_sensor` | `night_light` | `_night_light` | `bool` |
| `sensor` | `fan_level` | `_fan_level` | 整数 `1..5`，自定义单位“档” |
| `sensor` | `ai_target_temperature` | `_ai_target_temperature` | 整数 `30..42`，温度设备类别，单位 `℃` |

当前模式的内部状态键保持英文稳定值，由 `strings.json` 和 `zh-Hans.json` 提供实体名及枚举状态
翻译。小夜灯是只读 binary sensor，不与现有主照明 `light` 实体合并。

`_PLATFORMS` 增加 `Platform.SENSOR`。只有启用 WSS 的 Config Entry 创建四个新增实体。由 WSS
切换到 HTTPS-only 时，平台 setup 必须精确删除四个对应的旧 entity registry 项和当前 state，
与现有 connectivity 清理语义一致。

可共享一个只服务于面板状态实体的轻量基类，统一 Coordinator listener、DeviceInfo、unique ID
和可用性；不借机重写现有照明控制或 connectivity 实体。

## Diagnostics 与隐私

Diagnostics 可以增加以下固定、规范化字段：

- `panel_mode`：七个已知状态、`unknown` 或 `unavailable`；
- `night_light`：`true`、`false` 或 `null`；
- `fan_level`：`1..5` 或 `null`；
- `ai_target_temperature`：`30..42` 或 `null`；
- `panel_state_available`：当前 WSS 连接且至少一个正式字段已由当前连接确认并有效时为 `true`。

Diagnostics 不包含 service/property 路径、未知原始模式整数、倒计时候选、device ID、topic、
client token、payload、时间序列或探针信息。所有字段通过既有 fail-closed 安全归一化函数输出。

## 临时探针删除

以下生产表面在最终代码中不存在：

- `custom_components/aupu_q360/probe.py`；
- `custom_components/aupu_q360/services.py`；
- `custom_components/aupu_q360/services.yaml`；
- `PanelStateProbe`、runtime `probe` 字段和 probe stopper；
- probe observer/cancel、`probe_available`、`async_prepare_probe_transport()`；
- 带 `clientToken` 的探针专用 `async_request_shadow_get()`；
- `start_probe`、`sample_probe`、`stop_probe` Action；
- 五个 probe 错误码及服务翻译；
- `docs/q360-read-only-discovery-runbook.md`；
- `tests/test_probe.py`、`tests/test_probe_network_boundary.py`、`tests/test_services.py`。

WSS 建连后的空 `{}` Shadow `get`、正式 Shadow parser、照明状态处理、connectivity、认证和重连
必须保留。历史 `docs/superpowers/specs/` 与 `docs/superpowers/plans/` 是决策记录，不因生产探针
删除而改写或删除。

## 版本与用户文档

- `manifest.json` 与 `pyproject.toml` 同步升级为 `0.3.0`。
- README 的“临时只读状态探针”改为“只读面板状态”，列出四个实体、reported-only 边界、
  HTTPS-only 限制和不支持控制的范围。
- README 不宣称已支持剩余时间，也不把合成测试描述成真实设备控制验证。
- strings 与简体中文翻译只保留正式实体、现有配置流程和既有 Repair 文案。

## 测试策略

实施严格按测试先行推进，至少覆盖：

1. **Shadow 纯函数**：七个已知模式、未知整数、夜灯、五档、温度边界、partial update、desired
   忽略、错误类型、越界值、目标设备限制和无关字段忽略。
2. **Coordinator**：同一消息先应用照明、再应用面板状态且只通知一次；缺失保留、无效清空、
   断线不可用、重连 get 恢复、鉴权失败和 stop 不泄漏任务。
3. **实体平台**：四个实体的 unique ID、translation key、设备归属、类型、单位、枚举选项、
   listener 生命周期、可用性，以及 WSS-to-HTTPS registry/state 清理。
4. **Diagnostics**：只含固定规范化值；恶意属性、错误类型、未知模式和 secret-like 输入均
   fail closed，且不存在 discovery/probe 数据。
5. **删除和网络边界**：生产包、services/strings/文档和 runtime 不再引用 probe；WSS 仍只有
   一条连接、只保留建连空 get，正式面板状态不调用 HTTPS 控制或发布 Shadow update。
6. **真实 HA runtime**：真实 Config Entry manager 创建五个 WSS 状态实体（connectivity 加四个
   面板实体），合成 Shadow 消息更新状态，HTTPS-only 不创建或遗留这些实体，unload 无任务泄漏。
7. **完整门禁**：全量 pytest、HA runtime、Ruff lint/format、strict mypy、lock 校验、私有 signer
   验证、敏感信息扫描、manifest/翻译断言和 `git diff --check`。

所有自动测试只使用合成 device ID、token、topic 和 payload，不访问真实账号、HA token 或设备。

## 提交与部署

实施计划应把解析、Coordinator、实体、探针删除和文档/版本拆为可独立审查的测试先行提交。
最终全量验证通过后才能推送 `main`。

本机 HA 部署流程：

1. 重新核实仓库 HEAD、远端、工作树、容器、`/config` 挂载和当前组件路径。
2. 在现有 `.codex-backups` 下创建唯一、明确命名的部署前备份，不覆盖历史备份。
3. 精确替换整个 `custom_components/aupu_q360`，不能用只覆盖不删除的方式留下旧 probe 文件。
4. 恢复运行目录既有 `root:root`、目录 `0755`、文件 `0644`，并逐字节核对仓库与运行目录。
5. 在容器内运行 HA `check_config`，成功后重启 `homeassistant`。
6. 通过容器健康、HTTP、Recorder 和日志只读核实加载结果，不向设备发送控制命令。

部署后预期真实状态为：connectivity 在线、当前模式关闭、小夜灯关闭、风量 5 档、AI 目标温度
36℃；这些当前值必须以重启后的新 Shadow `reported` 为准，不能仅依赖实验结束时的人工报告。
同时验证三个 probe Action 已消失，AUPU setup、WSS 和正式解析无错误。

部署成功后保留一份最新部署前回滚备份。只删除本次已确认的临时客户端和已确认空的探针目录；
不删除其他 `.aupu_q360-backup-*` 或历史 `.codex-backups`，不存在的 Store/mount 不伪装成已清理。

## 回滚

如果配置检查、加载、实体状态或日志验证失败：

1. 不操作真实设备；
2. 用本次部署前备份精确恢复组件目录；
3. 重新核对权限和字节；
4. 运行 HA `check_config` 并重启；
5. 只读验证原照明和 connectivity 功能恢复；
6. 保留失败证据的固定错误摘要，不输出凭据或原始 Shadow。

## 完成标准

1. 只有上述四条真实证据支持的面板状态成为正式实体，持续时间和背景候选均未映射。
2. 当前模式用单一 enum sensor 表达互斥关系；夜灯、风量和温度均为只读状态。
3. partial update、未知/无效值、断线、重连和 HTTPS-only 行为有明确且通过的测试。
4. 临时 probe 模块、Action、运行手册、翻译、runtime 接线和专项测试全部删除。
5. 正式状态路径只消费目标 `reported`，不增加控制 API、第二条 WSS、持久化或原始数据表面。
6. Diagnostics 只输出固定规范化语义，仓库和部署均无 token、payload、Store 或原始档案残留。
7. 版本 `0.3.0` 通过完整自动验证，已提交并推送，HA 运行组件与仓库逐字节一致。
8. HA 重启后的真实 `reported` 状态、实体数量、日志和 probe Action 删除均经只读验证。
