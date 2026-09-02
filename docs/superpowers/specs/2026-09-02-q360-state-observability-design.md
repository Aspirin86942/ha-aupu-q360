# Q360 状态优先观测设计

## 目标

在现有持续 AWS IoT Shadow WSS 通道上，让 Home Assistant 明确区分设备确认状态、命令推定状态和断线后的过期状态。用户可以主要查看灯状态，而不会把缓存或目标值误认为实时物理状态。

## 范围

- 保留现有 Q360T5-Pro 照明开关能力。
- 不增加定时轮询；继续使用持续 WSS 推送。
- 首次连接和每次重连后各发送一次 Shadow `get`。
- 不增加取暖、换气、烘干等设备功能。
- 不持久化频繁状态或确认时间到 Config Entry。

## 实体

### 灯实体

现有灯实体继续显示最新开关状态，并增加以下非敏感属性：

- `state_source`：`reported`、`get_reported`、`desired`、`command` 或 `unknown`。
- `state_stale`：当前显示值是否可能过期或未经设备确认。
- `last_confirmed_at`：HA 收到最近一次设备确认状态的 UTC ISO 8601 时间；从未确认时为 `null`。
- `wss_connected`：当前 Shadow 会话是否完成订阅。
- `wss_healthy`：当前会话是否通过最近一次 MQTT 心跳确认。

灯实体在 WSS 断线时保留最后状态，不标记为 unavailable；此时 `state_stale=true`。HTTPS 控制能力不依赖 WSS，继续可用。

### 状态通道二进制传感器

启用 WSS 时新增一个与同一设备关联的 connectivity binary sensor：

- WSS 完成订阅后为 on。
- 断线、停止或重连期间为 off。
- 属性包含 `healthy`、`state_stale` 和 `last_confirmed_at`。
- HTTPS-only 模式不创建该实体。

## 状态规则

1. 收到 `reported` 或 `get_reported`：更新灯状态，设置 `state_stale=false`，并以 HA 接收时间更新 `last_confirmed_at`。
2. 收到 `desired` 或本地控制成功：更新灯显示值，设置 `state_stale=true`，保留原 `last_confirmed_at`。
3. WSS 断线：不清除灯值或确认时间，设置 `state_stale=true`，连接传感器变为 off。
4. WSS 重连：重新订阅并发送一次 Shadow `get`；收到新的 `reported` 后恢复可信状态。
5. 尚未收到任何状态时：灯值为 unknown，来源为 `unknown`，`state_stale=true`，确认时间为 `null`。

`last_confirmed_at` 只表示 HA 收到确认的时间，不表示浴霸内部产生事件的精确时间。HA 重启后该值从空开始，由首次 Shadow `get` 重建。

## 连续性与失败处理

沿用现有生命周期：30 秒 MQTT 心跳、10 秒 PINGRESP 截止时间，以及 2、4、8、16、30 秒退避重连。连接失败不清除最后状态；鉴权失败继续进入现有 Reauth。所有异常继续使用固定脱敏错误，不记录 WSS 凭据、Token 或设备标识。

## 实现边界

- `AupuCoordinator` 是状态来源、确认时间和 stale 判定的唯一权威。
- `light.py` 只投影协调器状态，不自行推断时间或连接健康度。
- 新建 `binary_sensor.py`，只投影协调器的连接状态。
- `__init__.py` 同时转发和卸载 `LIGHT`、`BINARY_SENSOR` 平台。
- 使用可注入 UTC clock，使确认时间测试不依赖真实时间。

## 验收测试

- `reported/get_reported` 更新时间并清除 stale。
- `desired/command` 不更新时间且保持 stale。
- 断线保留最后状态和时间，同时连接传感器关闭。
- 重连后的 `reported` 恢复可信状态。
- WSS 模式注册一个灯和一个连接传感器；HTTPS-only 模式只有灯。
- 两个实体共享设备注册信息，unique ID 稳定且不含真实设备 ID。
- 卸载清除两个实体监听器和全部 WSS 后台任务。
- Linux HA runtime 测试验证真实平台转发、实体注册、状态属性和卸载。
- 所有测试使用合成 transport，不连接真实云、短信或设备。

## 发布边界

本设计完成后先在本地分支实现和验证。提交、推送、Release 和真实设备测试仍分别受用户授权控制；不得因合成测试通过而宣称浴霸面板变化已经实机上报。
