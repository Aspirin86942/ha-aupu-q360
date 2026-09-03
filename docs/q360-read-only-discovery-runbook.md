# Q360 面板状态发现 v2 运行手册

本手册用于一次性识别 Q360 Shadow `reported` 中的面板状态候选。start 会先续建 WSS，并在确认新连接 healthy 后才建立发现基线；
任何时刻仍只有一条 WSS 连接，发现只发送相关联的 Shadow
`get`。所有模式、全局档位和 AI 目标温度操作均由操作者手工完成；操作者控制面可以是实体面板、
奥普官方 App 或官方微信小程序。
Home Assistant 不会替操作者开启、关闭、切换或恢复任何功能。

结果是 schema 2 脱敏候选报告，不会修改 Config Entry、自动创建实体或启用新控制。
`desired`、Action 阶段、用户陈述和命令结果都不是确认状态。官方控制面显示只用于核对人工
操作，不能作为字段证据；只有目标设备 Shadow `reported` 可以确认字段语义。正常流程不需要
HAR、SAZ、PCAP，也不得把任何原始发现内容放入 Git、HA 诊断、聊天或公开制品。

## 独立授权门

下列操作互不授权，必须在每一步执行前重新检查现场状态并取得当前明确授权：

1. 审查本地提交，以及分别授权合并和推送；
2. 创建宿主机私有目录；
3. 备份并修改 Compose，加入固定 bind mount；
4. 同步组件到实际 HA `/config/custom_components`；
5. 重建或重启 Home Assistant 容器并做无发现烟测；
6. 完成远程预检后，执行真实远程会话。

完成本地测试或前一个授权门不代表后一个授权门已获准。不要把本功能部署与凭据轮换、HA
升级、无关 Compose 清理或档案删除合并执行。

## 私有原始档案边界

原始档案是可选项，默认关闭。它只使用以下固定位置，不接受用户输入路径：

- 宿主机根目录：`/home/george/.local/state/ha-aupu-q360/raw-discovery/`
- Home Assistant 容器根目录：`/var/lib/aupu-q360-private-discovery/`

宿主机根目录和每个会话目录必须是 `0700`，档案与 manifest 文件必须是 `0600`。每个会话的
编码 JSONL 上限为 64 MiB；达到上限即失败关闭，不轮转、不上传、不自动删除。Base64 只是编码，
不是脱敏；解码后仍是原始私有数据。不要列出、打印、复制或分享 topic、payload、Base64 内容。

启用选项但固定挂载缺失、权限错误或不可写时，`start_discovery` 会在任何发现请求前失败。
HA Store 和 Diagnostics 只保存 schema 2 脱敏报告及允许的档案元数据，不保存文件路径或原始
内容。删除 Config Entry 不会删除宿主机档案；宿主机档案只能按单独授权的保留策略处理。

## 本地实施验证

在隔离的 Linux checkout 中运行：

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

这些测试使用合成凭据、内存 WSS、临时档案目录和本地 HA runtime。全部通过只证明本地程序
边界成立，不证明真实字段语义，也不授权合并、推送、部署、重启或真实远程操作。

## 部署授权门操作要求

### 创建宿主机私有目录

获得授权后，先解析精确路径并证明它不在仓库、HA `/config` 或 HA 备份目录内；拒绝符号链接，
再以 `0700` 创建并核对类型、权限和所有者。不要预先创建任何会话子目录，也不要读取已有档案。

### 修改 Compose

获得独立授权后，重新定位实际 Compose 项目和 HA 服务。保留权限、所有者与哈希可核对的备份，
只为 HA 服务加入一个读写挂载：

```yaml
- /home/george/.local/state/ha-aupu-q360/raw-discovery:/var/lib/aupu-q360-private-discovery:rw
```

运行 `docker compose config --quiet` 并核对解析后的 source、target 和读写模式后停止。Compose
解析成功不授权同步组件或重建容器。

### 同步组件

获得独立授权后，只从已验证提交的 `custom_components/aupu_q360` 构造候选目录。排除缓存、
测试、Git 元数据、Config Entry 材料和所有档案；核对源与候选 SHA-256 清单。保留 live 组件的
同文件系统可恢复备份，并通过原子目录换名同步。不要递归删除，也不要使用 `rsync --delete`。

同步后运行 HA 配置检查。失败时恢复备份并重新检查；成功时也只报告“文件已同步，运行中的
HA 尚未加载新代码”。

### 重建或重启 Home Assistant 容器

获得独立授权后，重新核对 live 文件哈希和 Compose 固定挂载，只重建实际 Home Assistant
服务，不升级镜像、不操作无关容器。恢复后确认 HA 版本未变化、Config Entry 已加载、原照明
实体与 connectivity 实体正常、WSS 状态语义无回归、固定挂载可见且权限正确。

烟测期间原始档案选项必须保持关闭，不得创建会话目录或开始发现。部署成功不授权执行真实
远程会话。

## 固定实验目录

真实会话只接受以下十个实验标签：

| 实验 | 含义 | 固定手工流程 |
| --- | --- | --- |
| `ai_thermostatic_warmth` | AI 恒温暖模式 | 开启，再关闭并刷新确认 |
| `deodorization_sterilization` | 除臭除菌模式 | 开启，再关闭并刷新确认 |
| `ventilation` | 换气模式 | 开启，再关闭并刷新确认 |
| `air_blowing` | 吹风模式 | 开启，再关闭并刷新确认 |
| `normal_drying` | 普通干燥模式 | 开启，再关闭并刷新确认 |
| `thermostatic_drying` | 恒温干燥模式 | 开启，再关闭并刷新确认 |
| `night_light` | 小夜灯模式 | 开启，再关闭并刷新确认 |
| `global_fan_level` | 共享全局档位 `1..5` | 固定以换气模式为载体 |
| `ai_target_temperature` | AI 目标温度 `30..42` | 固定以 AI 恒温暖为载体 |
| `idle_environment` | 全部模式关闭时的静置环境 | 不操作控制面 |

七个模式、静置环境、全局档位的四个非源目标、以及同一对相邻温度都必须各完成 round 1 和
round 2。不得猜测不存在的档位，不得换用其他载体，也不得在一个阶段同时操作多个变量。

## 执行真实远程会话

### 远程会话前检查

只有取得本授权门的当前授权，并同时满足以下条件时才继续：

- 操作者预留连续 45–60 分钟，并保持手机、电脑 HA 和聊天在线；
- 浴室无人、无宠物、设备没有被衣物或其他物品覆盖；
- 家中人员知道设备位置、停止方式和既有安全断电方式，并在会话期间保持可联系；
- 其他人不会操作设备，可能控制 Q360 的自动化已暂停；
- 运行组件哈希和固定挂载均已复核，WSS connected/healthy，connectivity 与灯光实体正常，
  AUPU 相关错误日志为空；
- 在官方控制面私下记录原始全局档位和原始 AI 目标温度，不在聊天或日志中输出；
- 在官方控制面关闭七个模式，刷新或重开页面后确认仍全部显示关闭；
- 如需原始档案，先在 Options 启用；这次 reload 不得自动开始发现；
- 准备在每次手机操作并刷新后等待 15–30 秒，并在 900 秒阶段期限内调用下一次 Action。

`all_modes_off_confirmed: true` 只表示操作者完成了上述关闭检查，不是字段证据。任何前提不成立
都不得调用 `aupu_q360.start_discovery`。

在“开发者工具 → 操作”中选择目标 Config Entry。调用 `aupu_q360.start_discovery`，并把
`all_modes_off_confirmed` 设为 `true`。WSS 续建与健康确认最多等待 45 秒，完成后才打开可选档案、
挂载 observer 并发出 10 秒基线请求。只有响应 `discovery_ready_for_step` 才可继续；没有相关
快照或返回任何其他错误时立即停止。

每个等待操作者的阶段最多 900 秒，完整发现会话最多 3,300 秒。
旧 v2 报告中的 120/3,600 和 300/3,300 超时 profile 只用于读取兼容，不是当前运行时限；新会话不会采用旧 profile。

每次 `begin_discovery_step` 后，只在官方控制面执行当前提示要求的一个操作；刷新或重开页面，
确认它显示刚请求的状态，等待 15–30 秒，再调用 `aupu_q360.advance_discovery_step`。不得同时
改变其他模式、档位或温度。只有 `discovery_cycle_recorded` 才允许开始下一轮。

### 静置环境两轮

对 `idle_environment` 的 round 1、round 2 分别执行：

1. 调用 `aupu_q360.begin_discovery_step`；
2. 保持控制面和设备状态不变，等待 15–30 秒；
3. 调用 `aupu_q360.advance_discovery_step`；
4. 只有 `discovery_cycle_recorded` 才进入下一轮。

### 七个模式各两轮

对每个模式标签的 round 1、round 2 分别执行：

1. begin 后等待 `discovery_prompt_mode_on`；
2. 只在官方控制面开启该模式，刷新确认、等待后 advance；
3. 等待 `discovery_prompt_mode_restore`，只关闭该模式并刷新确认；
4. 再次 advance；只有 `discovery_cycle_recorded` 才进入下一周期。

夜灯会影响现有照明状态路径，但仍只按同一手工恢复规则操作。官方控制面显示不会替代
Shadow `reported` 字段证据。

### 全局档位矩阵

以私下记录的原始档位作为 `source_level`，将 `1..5` 中其他四个值依次作为 `target_level`，
每个目标做两轮：

1. begin 后在官方控制面开启换气，刷新确认、等待后 advance；
2. 把档位从 source 改为 target，刷新确认、等待后 advance；
3. 恢复原始全局档位，刷新确认、等待后 advance；
4. 关闭换气并刷新确认，等待后 advance。

每轮都必须依次收到 carrier、parameter change、parameter restore、carrier off 的固定提示，
最终只接受 `discovery_cycle_recorded`。

### AI 目标温度两轮

以私下记录的原始温度作为 `source_temperature`，选择同一个合法相邻值作为
`target_temperature`：源值为 `30` 时用 `31`，源值为 `42` 时用 `41`，其他值固定选择一个相邻
方向，两轮不得反向或换目标。

1. begin 后在官方控制面开启 AI 恒温暖，刷新确认、等待后 advance；
2. 把温度从 source 改为 target，刷新确认、等待后 advance；
3. 恢复原始 AI 目标温度，刷新确认、等待后 advance；
4. 关闭 AI 恒温暖并刷新确认，等待后 advance。

### 恢复不确定与停止条件

状态机仍允许现场操作者在 `discovery_restore_required` 后纠正状态并重新推进同一阶段；纯手机
远程会话不得利用同阶段重试能力。该响应表示 `reported` 尚未确认恢复，必须中止并按恢复不确定
处理。

Action 返回非预期结果、`discovery_restore_required`、`discovery_manual_restore_required`、
超时、WSS 断线或不健康、鉴权/档案/资源/payload 错误、官方控制面失联或显示不一致、必要在线
链路失联、设备行为异常，或者无法确定恢复状态时：

1. 立即停止继续操作或开始新阶段；
2. HA 可用时调用 `aupu_q360.cancel_discovery`；取消不会控制或恢复设备；
3. 当前载体仍开启时，先恢复原档位或原温度，再关闭全部模式，并刷新或重开页面；
4. 载体已经关闭、官方控制面状态不确定，或恢复需要重新开启模式时，不再远程尝试，由现场人员
   接管；
5. 无论官方控制面显示什么，都让家中人员现场确认设备已停止发热、送风及其他运行；
6. 无法远程恢复或现场状态不明确时，由家中人员使用实体控制面停止设备，必要时按家中既有
   安全方式切断该设备供电；
7. 只记录固定错误码、阶段和明确时间戳；保留 incomplete 私有档案，不读取、输出或删除内容；
8. 不自动重试或继续原会话；再次实验必须重新预检并重新取得真实设备实验授权。

HA 已失联时不能调用 cancel，但仍须立即停止实验；只有当前载体仍开启且控制面状态明确时，才按
上述顺序远程恢复，否则直接通知现场人员接管。恢复 HA 后只做只读核查。取消不会覆盖上一次
成功报告，也不会删除已接受的私有档案内容。

### 完成与本地核验

只有会话回到 ready 且没有待恢复阶段时，调用 `aupu_q360.finish_discovery`。随后在官方控制面
确认七个模式全关、原始全局档位和原始 AI 目标温度均已恢复；正常完成不强制现场人员逐步骤
检查。成功响应只返回固定计数；随后在 Diagnostics 中确认报告 `schema_version` 为 `2`、敏感
扫描通过、覆盖状态与预期一致。Diagnostics 不应出现设备标识、Entry 标识、原始 topic/payload、
令牌、Base64 或路径。

如已启用档案，只在宿主机本地核对会话目录/文件权限、manifest 状态、事件数、编码字节数和
SHA-256；不得显示文件内容。删除 Config Entry 后，脱敏 Store 报告会被删除，但宿主机档案仍
按独立保留策略存在。

`confirmed_candidate` 仍只是候选。`ambiguous`、`observed_unidentified`、`not_observed` 和
`invalid` 不得映射为正式实体。任何正式只读实体都需要另行规格、实现、测试和部署授权。
