# Q360 发现传输窗口与远程超时修复设计

## 文档状态

- 日期：2026-09-03。
- 状态：设计已确认，等待本地 TDD 实施。
- 目标版本：`0.2.2`。
- 适用范围：Q360 面板状态发现 v2；不改变正式实体或控制能力。

## 问题与证据

真实远程实验暴露了两个相互独立的问题：

1. AWS IoT WSS 在连续两次观测中约每小时重建一次。发现会话若恰好在连接窗口末端启动，
   `start_discovery` 可以成功取得基线，但随后连接断开会按现有 fail-closed 规则清理会话，下一次
   Action 只能得到 `discovery_invalid_transition`。
2. 远程操作者通过聊天接收指令、切换手机控制面、刷新并回复，可能超过当前 120 秒阶段期限。
   已观测到一个阶段在基线后 120 秒到期，目标模式的 `reported` 更新随后才到达，因此推进 Action
   同样只能得到 `discovery_invalid_transition`。

设备恢复已经由现场人员确认；两次失败都没有产生成功报告，原始档案保持关闭。修复必须先在
合成测试中证明有效，不能用再次真实实验代替实现验证。

## 目标

1. 每次发现会话从一个新建且已确认健康的 WSS 连接窗口开始。
2. 给远程操作者最多 300 秒完成每个阶段，同时保留有限、明确的安全截止时间。
3. 将一次发现会话限制为 3,300 秒，在观测到的约一小时连接轮换前保留约五分钟余量。
4. WSS 续建、超时或后续断线均保持 fail-closed，不跨连接拼接发现证据。
5. 保持五个 discovery Actions、报告 schema 版本、Store key、实验目录和只读网络边界不变。
6. 继续兼容已经保存的旧 v2 `120/3600` 报告。

## 非目标

- 不调用奥普官方 App、微信小程序或设备控制 API。
- 不发送 Shadow `update` 或写入 `desired`。
- 不让活动发现阶段跨 WSS 断线恢复。
- 不增加 pause、resume、checkpoint 或新的 Action 参数。
- 不改变字段候选规则，不创建正式模式、档位或温度实体。
- 不读取、显示、记录或提交 Token、Cookie、Config Entry ID、原始 topic/payload 或档案内容。

## 采用方案

### 新鲜 WSS 窗口

`AupuShadowWebSocket` 增加一个串行化的续建方法。该方法必须：

1. 获取生命周期锁，禁止并发 start、stop 或 renew 创建多个 runner；
2. 完整停止并等待旧 runner、socket、ping task 和 receive task；
3. 清除本地健康事件；
4. 启动唯一的新 runner，使其重新获取一次性 WSS 凭据并重新订阅两个 accepted topic；
5. 等待新连接首次 PINGRESP，最长 45 秒；
6. 成功时保证 `is_running=true` 且健康事件只来自本次新连接。

等待健康超时时不伪造 connected/healthy，也不泄漏底层异常。新 runner 可以继续走既有重连循环，
但 discovery start 必须失败，且不得挂载 observer 或发送发现基线。

### Coordinator 边界

`AupuCoordinator.async_prepare_discovery_transport()` 是 discovery 唯一使用的准备入口。它拒绝以下
状态：runtime 已停止、正在 Reauth、WSS 未启用或缺少 WSS 用户标识。通过后调用 WSS 续建方法，
并最终核对 coordinator 的 `wss_connected` 与 `wss_healthy`。

正常实体状态继续通过现有 connection callback 更新。续建期间状态通道可以短暂显示断开或不健康，
但不能清空或反转最后一个正式灯光状态。

### Discovery 启动顺序

`PanelStateDiscoverySession.async_start()` 固定按以下顺序执行：

1. 校验状态为 idle，且 `all_modes_off_confirmed is True`；
2. 进入内部 transport-preparing 状态并等待 coordinator 准备新 WSS 窗口；
3. 再次确认 discovery transport 可用；
4. 创建 sanitizer；仅在启用时打开原始档案；
5. 挂载 observer；
6. 启动 3,300 秒会话 timer；
7. 发送关联 Shadow `get` 并在 10 秒内取得完整目标 `reported` 基线；
8. 返回现有 `discovery_ready_for_step`。

准备阶段失败统一映射为 `discovery_wss_unavailable`。由于 observer、档案和会话尚未开始，失败后
回到 idle，`manual_restore_required=false`，且不要求设备恢复。

### 时间限制

- WSS 健康准备：45 秒；不进入报告 limits。
- Shadow 快照：10 秒，保持不变。
- 操作者阶段：300 秒，由每个新提示重置。
- 完整发现会话：3,300 秒，从 observer 挂载并开始会话基线时计算，阶段推进不得重置。
- 变化数、MQTT 包和原始档案大小限制保持不变。

3,300 秒是硬截止时间，不承诺完整目录一定能在慢速操作下完成。运行手册要求操作者提前打开
官方控制面，每个提示出现后尽快完成单变量操作，并由自动调用方在收到“完成”后等待 15–30 秒
再推进。

## 报告兼容性

新报告继续使用 `schema_version: 2`，结构不变，并写入新 limits：

```json
{
  "snapshot_timeout_seconds": 10,
  "stage_timeout_seconds": 300,
  "session_timeout_seconds": 3300,
  "max_changes_per_phase": 256,
  "mqtt_packet_bytes": 65536,
  "raw_archive_bytes": 67108864
}
```

验证器只接受两个完整且不可混搭的 profile：旧 `120/3600` 或新 `300/3300`。其余四个限制在两组
中完全相同。这样旧 v2 Store 报告和 Diagnostics 仍可读取，同时拒绝部分修改或任意放宽的 limits。

## 错误与清理

- WSS renew 失败或 45 秒未健康：start 返回 `discovery_wss_unavailable`，无 observer、timer、档案
  或发现 Shadow 请求残留。
- renew 被外部取消：等待旧/new runner 的清理完成后传播取消，不吞掉调用方取消。
- 活动会话中的 WSS 断线、鉴权失败或 HA stop：沿用现有统一 abort；若已有未确认恢复路径，保留
  `manual_restore_required=true`。
- 300 秒阶段超时：沿用 `discovery_step_expired` 和现场恢复规则。
- 3,300 秒会话超时：沿用 `discovery_session_expired` 和现场恢复规则。
- 正常 cancel/finish：精确卸载 observer、取消 timers 并清理 session key。

## 测试策略

实施必须测试先行，并全部使用合成凭据、fake WSS、内存 Shadow 和临时目录：

1. WSS renew 完整停止旧连接，只创建一个新 runner，并在新连接首次 PINGRESP 后返回。
2. renew 等待健康超时或被取消时没有 task/socket 泄漏，也不会把旧连接健康事件误认为成功。
3. coordinator 准备方法拒绝停止、Reauth、缺少 WSS 的状态，并在成功后保持正式灯光状态。
4. discovery start 在 transport prepare 完成前不得打开档案、挂载 observer 或发送 Shadow get。
5. prepare 失败返回固定错误，资源全清，`manual_restore_required=false`。
6. 真实 HA runtime fake WSS 路径验证 start 会先 renew，再完成关联基线。
7. 新报告生成 `300/3300`；验证器接受旧、新完整 profile，拒绝混搭。
8. 现有网络守卫继续证明 discovery 没有控制 API 或 Shadow update/desired。
9. 全量 pytest、HA runtime pytest、Ruff、format、mypy、私有签名检查、敏感信息扫描和
   `git diff --check` 全部通过。

## 文档与版本

README、运行手册、两份远程发现设计的现行说明更新为：start 会先刷新并确认 WSS；每阶段 300 秒；
会话 3,300 秒。`pyproject.toml`、`uv.lock`、manifest、常量和版本测试统一升级为 `0.2.2`。

## 部署与真实实验边界

本地修改和测试不自动授权提交、推送、同步 HA、reload/restart 或真实设备实验。部署时仍须先备份、
同步并比较字节，再经 Home Assistant 配置检查和单独授权的重启生效。部署后先做无 discovery 烟测；
只有新版本、连接续建和实体状态均验证通过，才能重新进行真实远程实验。
