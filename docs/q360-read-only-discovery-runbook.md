# Q360 只读状态发现运行手册

本手册用于一次性识别 Q360 `reported` 状态中的候选字段。发现会话复用集成已有的唯一
AWS IoT WSS 连接，只发送带关联令牌的 Shadow `get`；取暖、换气、烘干、摆风、档位和
定时的实验动作全部由用户在奥普实体面板上手工完成，Home Assistant 不发送这些控制。

发现结果只是脱敏候选报告，不会修改 Config Entry、自动创建实体或启用新的控制能力。
原始 HAR、SAZ、PCAP 或 Shadow 报文不是运行依赖，也不得放入 Git、诊断分享或操作记录。

## 授权阶段

下面四个阶段彼此独立，完成前一阶段不代表后续阶段已经获准：

1. Linux 开发 checkout 本地验证；
2. 获得明确授权后，将已验证的组件文件同步到 HA `/config`；
3. 再次获得明确授权后，重启 HA 并做原有功能烟测；
4. 再次获得明确授权后，连接真实 Q360 并执行发现会话。

本地 Git 仓库必须与 HA `/config` 分离。提交、推送、Release、同步、重启和真实会话也分别
需要明确授权。

## 阶段一：Linux 本地验证

在开发 checkout 中执行：

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

这些测试使用合成凭据、内存 WSS 和本地 HA runtime，不应访问真实 DNS、云账号、短信服务或
浴霸。全部通过只证明程序边界正确，不证明真实字段语义。

## 阶段二：获授权后同步组件

同步前重新确认精确的 checkout、提交版本、HA 容器、`/config` 挂载和现有组件目录。只同步
`custom_components/aupu_q360` 中经过验证的组件文件，不迁移 `.private/`、原始抓包、Cookie、
证书、测试缓存或仓库根目录的私有配置材料。

同步应采用可恢复流程：先建立候选目录并核对允许文件的清单与哈希，再保留现有 live 目录
作为备份，最后在同一文件系统内换名。同步后只运行 HA 配置检查；成功时停止，并明确报告
“文件已同步，但运行中的 HA 尚未加载新代码”。本阶段不包含重启授权。

如果配置检查失败，保留失败候选，恢复备份并再次检查。不要用递归删除、`rsync --delete`
或直接覆盖 live 目录。

## 阶段三：获授权后重启与烟测

重启前确认阶段二的 live 文件哈希仍与已验证 checkout 一致。获得独立重启授权后，重启 HA，
并核验：

- HA 恢复运行且版本未意外变化；
- `AUPU Q360` Config Entry 正常加载；
- 原照明实体和 WSS connectivity 状态通道无回归；
- 没有固定鉴权、协议或 runtime 错误。

本阶段不得自动开始发现，也不得调用浴霸控制。若出现启动回归，先恢复备份并通过配置检查；
再次重启到旧版本仍需新的重启授权。

## 阶段四：获授权后执行真实会话

### 会话前检查

- 运行中的组件文件哈希与已验证版本一致；
- WSS 已启用，connectivity 状态通道可用；
- 取暖、换气、烘干、摆风、档位和定时均处于关闭基线；
- 操作者已准备只改变实体面板上的一个变量；
- 已按私有材料标准备份 Config Entry，备份不进入 Git、聊天或 shell 输出。

在 Home Assistant“开发者工具 → 操作”中选择对应 Config Entry。先调用
`aupu_q360.start_discovery`。它会发送一次只读 Shadow `get`；只有返回
`discovery_ready_for_step` 才继续。10 秒内没有取得相关快照时停止，不连续重试。

### 单步固定顺序

每一个实验步骤都严格执行：

1. 调用 `aupu_q360.begin_discovery_step`，选择 `capability`、`target` 和 `round`；
2. 等待返回 `discovery_ready_for_panel_action`；
3. 只在浴霸实体面板改变这一个目标变量；
4. 等待 15–30 秒，不操作其他功能；
5. 调用 `aupu_q360.complete_discovery_step`；
6. 只有返回 `discovery_step_recorded` 才进入下一步。

`heating`、`ventilation`、`drying`、`swing` 和 `timer` 都执行两轮：每轮先完成 `on`，再完成
`off` 并确认实体面板恢复关闭基线。

`fan_level` 只测试实体面板实际存在的 `level_1`、`level_2`、`level_3`。每个可见档位做两轮，
每次档位观察后用 `target=off` 的步骤恢复基线；不存在的档位不猜测、不操作。

`idle_environment` 仅使用 `target=off`，在全部功能关闭时观察两轮，期间不改变面板。温度、
湿度、功率等数值没有独立语义证据时只能作为未识别变化，不能根据数值范围猜测字段含义。

### 完成、取消与报告

所有可靠步骤结束且当前没有活动步骤时，调用 `aupu_q360.finish_discovery`。成功响应只返回受控
计数，不返回报告正文；随后下载该 Config Entry 的 Home Assistant 诊断查看脱敏报告。

发生误操作、多个变量同时变化、断线、超时或资源限制时，调用
`aupu_q360.cancel_discovery`。取消只清理当前内存会话，不覆盖上一次成功保存的报告。只有
`finish_discovery` 完成全部校验并原子保存后，最新报告才会替换旧报告。卸载集成保留报告，
删除 Config Entry 才清除对应报告。

下载后先确认报告不含设备/Entry/实体标识、凭据、手机号、完整 Topic、client token、原始
字符串或原始 Shadow。任何命中都应停止分享和字段解释，回到本地缺陷修复流程。

`confirmed_candidate` 也只是待人工确认的候选。`ambiguous`、`observed_unidentified`、
`not_observed` 和 `invalid` 不得映射成正式实体。任何正式只读实体都需要另行规格、实现、测试
与授权。
