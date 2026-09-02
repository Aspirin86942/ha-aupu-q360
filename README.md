# 奥普 Q360T5-Pro Home Assistant 自定义集成

这是一个非官方、云端接入的 Home Assistant 自定义集成，用于控制用户自有的奥普
Q360T5-Pro 浴霸照明。云 API 与私有协议可能随厂商更新而失效，本项目不受奥普官方支持，
也不保证长期兼容。

## 支持范围

- 仅支持一个照明实体的 on/off 控制。
- 不支持取暖、换气、烘干、摆风以及浴霸的其他功能。
- 控制依赖奥普 HTTPS 云 API；AWS IoT WSS 仅用于接收状态反馈，不是本地控制通道。

## HACS 安装

项目仓库是 <https://github.com/Aspirin86942/ha-aupu-q360>。标准 HACS 自定义仓库安装要求
仓库可公开访问；如果该仓库仍为私有，不能仅凭 GitHub 登录状态让 HACS 下载它，也不要向
Home Assistant 或第三方提供 GitHub Token。此时可先手工把 `custom_components/aupu_q360`
复制到 HA 的 `custom_components` 目录，或由仓库所有者另行决定是否公开仓库。

满足公开访问条件后，按以下顺序安装：

1. 确认仓库已公开且可被 HACS 访问。
2. 对准备安装的提交运行 HACS 和 hassfest 验证，并以 GitHub Actions 最新结果为准。
3. 在 HACS 中添加仓库 URL `https://github.com/Aspirin86942/ha-aupu-q360`，类别选择“集成”，
   然后下载 `AUPU Q360`。
4. 按 HACS 提示重启 Home Assistant。
5. 重启后进入“设置 → 设备与服务 → 添加集成”，搜索 `AUPU Q360`。

## 添加集成

初次配置需要在 Home Assistant 本地输入以下值：

- 私有签名 JSON；
- Bearer JWT；
- 设备标识和设备标签；
- 是否启用 WSS 状态反馈（默认关闭）。

私有签名 JSON 通过本地导入/粘贴方式进入配置表单。以上值只应在 HA 本地输入，不要提交到
Git，不要发到 Issue、日志、截图或聊天。默认的 HTTPS-only 配置只做本地校验；启用 WSS 时，
界面会在二次确认后执行一次只读连接检查，不会发送照明控制。

## 凭据、JWT 与重新认证

JWT 会随 Config Entry 持久保存，因此 Home Assistant 重启后仍可使用；但 JWT 不是永久凭据，
集成不会自动刷新或自动续期。即将到期与已经到期分别显示 `jwt_expiring`、`jwt_expired`
Repair，控制会在凭据失效时停止。`jwt_expiring` 只是提前告警：到期前只能通过 Options 手工更新 Token；
即将到期告警不会启动 Repair fix flow 或短信 Reauth。

短信或手工 Reauth 只会在 Token 已过期、远端鉴权失败或缺少 WSS user UUID 时由 HA 启动。
进入 Reauth 后可选择：

- 短信 Reauth：提交手机号后才发送一条验证码，同一 flow 内 60 秒限发；验证码 5 分钟后在
  本地过期且不会保存。集成不会自动读取短信。手机号仅在用户明确勾选时保存。
- 手工 Token：将新的 Bearer JWT 作为保底方式输入；旧 Token 不会显示，也不能复用旧值完成
  Reauth。

成功恢复后，集成以一次 Config Entry 更新替换完整候选凭据；失败不会覆盖原有配置。

## 状态与控制语义

- 每次用户开/关动作只通过 HTTPS 发送一次控制请求；失败后不会自动重放。
- WSS 只用于状态反馈，不做周期轮询；首次连接及每次重连后各执行一次 Shadow `get`。
- `reported` 与 `get_reported` 是设备确认；`desired` 与 `command` 是推定状态。
- WSS 断开或不健康不会反转最后状态，也不会触发第二次 HTTPS 控制；灯保留最后值，但
  `state_stale=true`，直到后续设备确认。
- `last_confirmed_at` 是 Home Assistant 接收设备确认的 UTC 时间；重启后由首次 Shadow `get`
  重建，不代表设备侧事件发生时间。
- 启用 WSS 时会创建状态通道 connectivity binary sensor；HTTPS-only 模式不创建该实体。

## 只读状态发现

启用且连接 WSS 后，可通过五个 `aupu_q360` Action 执行一次性的 v2 只读字段发现：
`start_discovery`、`begin_discovery_step`、`advance_discovery_step`、`finish_discovery` 和
`cancel_discovery`。发现复用现有 WSS 连接且只发送相关联的 Shadow `get`；Home Assistant 不会
开启、关闭或设置面板模式、档位与温度。只有目标设备 Shadow `reported` 是确认状态，
`desired`、操作阶段和用户陈述都不会被当作成功证据。

固定实验目录包含十个标签：

- 七个独立模式：`ai_thermostatic_warmth`、`deodorization_sterilization`、`ventilation`、
  `air_blowing`、`normal_drying`、`thermostatic_drying`、`night_light`；
- 共享全局档位：`global_fan_level`，合法值为 `1..5`，固定以 `ventilation` 为载体；
- AI 目标温度：`ai_target_temperature`，合法值为 `30..42`，固定以
  `ai_thermostatic_warmth` 为载体；
- 静置环境：`idle_environment`。

每个 begin 先取得步骤基线，之后由用户按返回的固定提示在实体面板手工操作，再用 advance
取得下一阶段快照；模式、档位目标和温度实验各做两轮，并在每轮内人工恢复原状态。取消只
清理本次软件会话，不会发恢复命令，也不会覆盖上一次成功报告。

Options 中的本机私有原始发现档案默认关闭，只能使用管理员预先配置的固定挂载；启用但挂载
不可用时，会在发送发现请求前失败。无论是否保留原始档案，HA Store 和诊断都只包含通过
schema 2 校验与敏感扫描的脱敏报告。报告不会自动修改配置、创建实体或启用新控制；候选字段
进入正式映射前仍需另行设计、测试和授权。原始 HAR、SAZ、PCAP 不是正常发现流程的运行依赖。

完整的授权阶段、实验矩阵、异常停止条件和报告审查要求见
[Q360 只读状态发现运行手册](docs/q360-read-only-discovery-runbook.md)。

## 安全与备份

HA 备份包含 Config Entry 中的 JWT、私有签名和设备配置。备份必须加密、限制访问，并按凭据
材料管理；恢复到另一台主机前也要确认接收方可信。不要把 `.private/`、`local-evidence/`、
原始 HAR、证书、Cookie 或测试日志放入 Git 或公开制品。

如果怀疑凭据泄露，应在厂商侧使旧凭据失效，并在 HA 中通过 Reauth 替换；不要把旧值发给
维护者排查。

## 升级与卸载

- 升级：在 HACS 中下载目标版本，按提示重启 Home Assistant，再确认集成已正常加载。
- 卸载：先在“设置 → 设备与服务”删除 `AUPU Q360` Config Entry，再在 HACS 中删除集成并
  重启 Home Assistant。删除前根据自己的备份策略处理包含 secret 的旧备份。

## 故障排查

先下载 Home Assistant 的集成诊断。诊断返回白名单健康字段，包括集成版本、粗粒度 JWT
到期区间、WSS 开关/连接/健康状态、推定状态、状态来源和固定错误码；存在已成功保存的只读
发现结果时，还会包含通过固定 schema 与最终敏感扫描的脱敏报告。常见固定错误码包括
`authentication_failed`、`rate_limited`、`temporary_failure`、`protocol_error` 和
`runtime_stopped`。

排查时只提供诊断白名单和固定错误码；不要公开 JWT、私有 signer、手机号、验证码、设备 ID、
WSS 查询参数或原始 HAR。鉴权错误先走短信 Reauth 或手工 Token；协议错误可能表示厂商已更新
私有 API，需要在不暴露凭据的前提下重新评估兼容性。

## 发布前说明

仓库元数据已经指向真实项目地址，并包含 HACS 配置、本地图标和许可证。HACS/hassfest 是否
通过以 GitHub Actions 对当前提交的最新运行结果为准。真实短信发送、手机号登录、真实照明
控制、Release 与 HACS 实机安装不属于自动验证范围，不能把合成测试称为真实浴霸验证。

协议边界与脱敏研究结论见
[脱敏协议分析](docs/research/q360t5-ha-analysis-redacted.md)。
