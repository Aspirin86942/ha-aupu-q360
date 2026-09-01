# 奥普 Q360T5-Pro Home Assistant 自定义集成

这是一个非官方、云端接入的 Home Assistant 自定义集成，用于控制用户自有的奥普
Q360T5-Pro 浴霸照明。云 API 与私有协议可能随厂商更新而失效，本项目不受奥普官方支持，
也不保证长期兼容。

## 支持范围

- 仅支持一个照明实体的 on/off 控制。
- 不支持取暖、换气、烘干、摆风以及浴霸的其他功能。
- 控制依赖奥普 HTTPS 云 API；AWS IoT WSS 仅用于接收状态反馈，不是本地控制通道。

## HACS 安装

当前仓库没有已确认的 GitHub 发布地址。发布者先把代码发布到自己真实的 GitHub 仓库，
再按以下顺序完成元数据验证和安装。不要把示例文字当成 URL，也不要为未创建的仓库编造
链接。

1. 发布到真实 GitHub 仓库。
2. 用该仓库的真实 URL 补充真实 `documentation` 与 `issue_tracker`，并把元数据作为一个
   单独提交。
3. 对该提交运行 HACS 和 hassfest 验证，确认通过后再进入安装步骤。
4. 在 HACS 中添加真实仓库 URL；填写“你的真实 GitHub 仓库 URL”，类别选择“集成”，然后
   下载 `AUPU Q360`。
5. 按 HACS 提示重启 Home Assistant。
6. 重启后进入“设置 → 设备与服务 → 添加集成”，搜索 `AUPU Q360`。

当前代码不会写入虚假或占位 URL；没有完成第 1 至 3 步时，不能把 HACS/hassfest 声明为
已经实证通过。

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
Repair，控制会在凭据失效时停止。

Repair 触发后可选择：

- 短信 Reauth：提交手机号后才发送一条验证码，同一 flow 内 60 秒限发；验证码 5 分钟后在
  本地过期且不会保存。集成不会自动读取短信。手机号仅在用户明确勾选时保存。
- 手工 Token：将新的 Bearer JWT 作为保底方式输入；旧 Token 不会显示，也不能复用旧值完成
  Reauth。

成功恢复后，集成以一次 Config Entry 更新替换完整候选凭据；失败不会覆盖原有配置。

## 状态与控制语义

- 每次用户开/关动作只通过 HTTPS 发送一次控制请求；失败后不会自动重放。
- WSS 只用于状态反馈。收到设备 `reported` 状态后才视为已确认；只看到命令或 `desired`
  状态时，实体保持推定状态。
- WSS 断开或不健康不会反转最后状态，也不会触发第二次 HTTPS 控制；灯会继续显示推定状态，
  直到后续确认。

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

先下载 Home Assistant 的集成诊断。诊断只返回白名单健康字段，包括集成版本、粗粒度 JWT
到期区间、WSS 开关/连接/健康状态、推定状态、状态来源和固定错误码。常见固定错误码包括
`authentication_failed`、`rate_limited`、`temporary_failure`、`protocol_error` 和
`runtime_stopped`。

排查时只提供诊断白名单和固定错误码；不要公开 JWT、私有 signer、手机号、验证码、设备 ID、
WSS 查询参数或原始 HAR。鉴权错误先走短信 Reauth 或手工 Token；协议错误可能表示厂商已更新
私有 API，需要在不暴露凭据的前提下重新评估兼容性。

## 发布前说明

当前完成标准是离线可安装代码、合成数据测试和 CI 定义，不包含真实环境发布。真实短信发送、
手机号登录、真实照明控制、GitHub 远程仓库创建、push、Release 与 HACS 实机安装均未执行，
每项都需要用户之后单独授权。没有真实 GitHub 仓库 URL 前，HACS/hassfest 的远程发布结果也
不能被声明为已实证通过。

协议边界与脱敏研究结论见
[脱敏协议分析](docs/research/q360t5-ha-analysis-redacted.md)。
