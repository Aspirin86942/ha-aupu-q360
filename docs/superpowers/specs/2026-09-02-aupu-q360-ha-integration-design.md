# 奥普 Q360T5-Pro Home Assistant / HACS 集成设计规格

日期：2026-09-02

状态：用户已于 2026-09-02 审查通过，准备实现

范围：Q360T5-Pro 照明开关，不包含取暖、换气、烘干及其他设备能力

## 1. 目标与成功标准

项目交付一个可由 GitHub/HACS 安装的 Home Assistant 自定义集成。用户在 HA 本地完成私有配置后，应获得一个 `light` 实体，并满足：

1. HA 重启后无需重新录入 JWT，未过期会话自动恢复。
2. 每次请求使用当前 Unix 秒动态生成已验证的 `App-Authorization`。
3. 开灯和关灯仅修改已确认的布尔字段，不触碰其他浴霸功能。
4. JWT 即将过期时提前告警；失效时停止控制并进入重新认证，而不是持续重试。
5. 支持用户在 HA 本地通过手机号和短信验证码重新认证；验证码不保存、不记录。
6. 优先使用 AWS IoT WSS Device Shadow 反馈实际状态；不可用时明确标记为推定状态。
7. GitHub/HACS 包及诊断输出中不存在账户、设备或签名秘密。
8. 自动化测试不访问奥普云端，也不真实控制设备。

## 2. 已确认的协议事实

控制请求为普通 HTTPS：

```text
POST https://cn-north-1-prod.aupu.net/appapi/iot/control
Authorization: Bearer <JWT>
App-Authorization: <dynamic-signature>
Content-Type: application/json
```

照明字段：

```text
sendBody.state.desired.{thing-id}.2.properties.1 = true | false
```

`did`、Topic 内的 thing ID 及 `desired` 动态键相同。开灯与关灯抓包除该布尔值外没有业务字段变化。

`App-Authorization` 已从本机小程序包静态恢复。其输入包含应用标识、SDK 版本、固定客户端类型和 Unix 秒；摘要为 HMAC-SHA256，随后按小程序格式编码和拼接。A/B/C 共 7 个请求的离线重算均逐字节匹配。真实常量不进入本规格。

JWT 没有已发现的 Refresh Token 或刷新接口。小程序在业务状态 `401/1017/1018` 时清除本地登录。已发现的合法登录路径为：

- 微信快速登录：依赖微信一次性临时码，不适合 HA 后台自动执行。
- 手机短信登录：先请求短信验证码，再以手机号和验证码换取 `{token, user}`；适合 HA 交互式重新认证，但必须在用户授权的受控测试中确认生产可用性。

状态通道为 MQTT 3.1.1 over AWS IoT WebSocket。连接凭据由奥普 `getWssToken` 动态下发，已确认 Shadow 的 `2.properties.1` 表示照明状态。

## 3. 方案选择

### 3.1 首版：持久化会话加短信重新认证（采用）

HA 将当前 JWT、签名参数、设备参数保存在本地 Config Entry 中。重启后恢复；到期前创建 Repair 提醒；过期或收到明确鉴权错误后触发 HA Reauth Flow。用户主动点击发送短信并在 HA 页面输入验证码，新 JWT 原子替换旧值。

该方案不承诺 JWT 永久有效，但能提供稳定的重启持久化和可审计的周期性重新认证。它不保存验证码，也不尝试绕过短信二次认证。

### 3.2 Windows 微信 Token Broker（未来可选）

常开 Windows 电脑可作为独立伴随服务：由微信电脑版重新打开小程序获得新会话，再将 JWT 经本地加密通道同步至 HA。它可能依赖代理 CA、运行时注入或微信 UI 自动化，容易受微信升级影响，并扩大凭据暴露面。

首版只预留 `CredentialProvider` 接口，不实现 Token Broker。若以后实现，必须作为独立可选组件，不得让 HACS 集成直接操纵微信进程。

### 3.3 局域网或设备侧 MQTT（未来研究）

如果以后恢复 Q360T5-Pro 的局域网协议或设备侧 MQTT 客户端证书，可绕开奥普账户 JWT，获得更强的长期可用性。目前只有设备到 AWS `8883/TLS` 的连接证据，没有客户端证书、私钥或本地控制帧，因此不能作为当前设计基础。

### 3.4 手工更新 JWT（保底）

Options Flow 始终允许用户在 HA 本地替换 JWT。这是短信接口不可用时的恢复手段，不是首选日常流程。

## 4. 仓库与 HACS 布局

计划目录：

```text
wechat-home/
├── custom_components/aupu_q360/
│   ├── __init__.py
│   ├── manifest.json
│   ├── const.py
│   ├── config_flow.py
│   ├── api.py
│   ├── auth.py
│   ├── signer.py
│   ├── coordinator.py
│   ├── light.py
│   ├── diagnostics.py
│   ├── strings.json
│   └── translations/zh-Hans.json
├── tests/
├── docs/
├── hacs.json
├── README.md
└── .gitignore
```

GitHub 仓库仅包含通用实现、合成测试 fixture 和脱敏文档。HACS 作为“Integration”类型安装；版本通过 Git tag / GitHub Release 管理。仓库发布、推送和公开性是后续独立操作，不能从本设计审批自动推断。

本机取证文件分两类：

- 可提交：脱敏分析报告、无硬编码常量的通用签名器。
- 仅本地：签名常量、JWT、设备参数、原始/脱敏 HAR、解包小程序和完整验证输出。它们放在 `.private/` 或 `local-evidence/`，由 `.gitignore` 排除。

## 5. 组件设计

### 5.1 `signer.py`

职责：从已经校验的私有参数对象生成 `App-Authorization`。

接口：

```python
class AppAuthorizationSigner:
    def sign(self, timestamp: int | None = None) -> str: ...
```

要求：

- 默认使用 `int(time.time())`，测试可注入固定时间。
- 不输出消息、密钥、摘要或完整请求头。
- 参数缺失或格式不符时 fail closed。
- 检测 HA 与服务器时间差；明显漂移时创建可读错误，不用旧签名重试。

### 5.2 `auth.py`

职责：规范化 Bearer JWT、只读解析 `exp`、判断会话状态并协调重新认证。

状态：

```text
UNCONFIGURED -> READY -> EXPIRING -> EXPIRED
                     \-> AUTH_REJECTED
EXPIRED/AUTH_REJECTED -> REAUTH_REQUIRED -> READY
```

规则：

- 接受纯 JWT 或 `Bearer <JWT>`，内部只保存一种规范形式。
- JWT 解析只用于本地到期判断，不把未验证 Claims 当作授权依据。
- 到期前 24 小时创建 Repair 提醒。
- 到期或服务端返回 `401/1017/1018` 后抛出 HA 鉴权失败，触发 Reauth Flow。
- 不以高频重试规避鉴权失败。

### 5.3 `config_flow.py`

初始配置步骤：

1. 导入本地签名参数 JSON。
2. 输入或粘贴当前 JWT。
3. 输入 `did/thing-id` 和 `tag`。
4. 只做本地格式校验；默认不发送网络请求。
5. 经用户单独授权后才能执行一次连接验证。

重新认证步骤：

1. 用户选择“短信重新认证”或“手工替换 JWT”。
2. 短信路径要求用户确认手机号并主动点击发送。
3. 集成调用短信接口，显示验证码输入页。
4. 验证码只存在于当前 Flow 内存中；流程结束或超时即丢弃。
5. 登录成功后验证返回结构，再原子更新 Token；失败不覆盖旧配置。

### 5.4 `api.py`

职责：构造奥普 HTTPS 请求并进行统一错误分类。

核心调用：

```python
async def set_light(is_on: bool) -> None:
    body = build_shadow_update(light_value=is_on)
    await request("POST", "/appapi/iot/control", json=body)
```

要求：

- 每次调用即时生成 `App-Authorization`。
- 不记录请求头、正文、响应正文或设备标识。
- HTTP 状态、业务 `status` 和网络错误分别分类。
- 对鉴权失败、限流、超时和服务端错误使用不同的可重试语义。
- 不自动重放控制请求，避免一次用户动作导致多次设备控制。

### 5.5 `coordinator.py` 与 WSS

职责：获取 WSS 凭据、连接 AWS IoT、订阅 Shadow Topic 并更新 HA 状态。

规则：

- WSS 凭据只保存在内存，不写入 Config Entry 或日志。
- 连接断开使用有上限的指数退避；鉴权失败转入 Reauth，不无限重连。
- 收到 Shadow `reported` 时作为优先真实状态；仅收到 `desired` 时不冒充物理确认。
- 首次连接发布一次 `shadow/get` 获取初始状态。
- 若 WSS 不可用但 HTTPS 控制成功，实体设置 `assumed_state = true`，直到获得真实反馈。

WSS 客户端的生产依赖选择必须在实现计划中单独确认。优先复用 Home Assistant 已有、稳定的异步能力；若必须新增依赖，需要在写入 `manifest.json` 前获得用户明确同意。

### 5.6 `light.py`

唯一实体：Q360T5-Pro 照明。

- `async_turn_on` 只提交 `2.properties.1 = true`。
- `async_turn_off` 只提交 `2.properties.1 = false`。
- 可用性由云端连接、JWT 状态和最近状态反馈共同决定。
- 不暴露取暖、换气、烘干等未经验证的服务。

### 5.7 `diagnostics.py`

诊断仅输出：集成版本、连接状态、到期剩余时间区间、最后错误类别、WSS 是否连接。必须删除或替换：JWT、签名参数、设备 ID、tag、手机号、WSS 查询参数、原始响应和精确时间戳。

## 6. 私密数据与备份风险

HA Config Entry 存储在本机 `.storage`，实现持久化但不是应用层加密保险箱。能够读取 HA 配置目录或完整备份的人可能获得设备控制能力。因此：

- 项目日志和诊断从源头不接收秘密值。
- GitHub Actions、Issue 模板和错误上报不上传配置。
- HA 备份应使用加密并妥善保管。
- 删除集成时提供清除 Config Entry 的正常路径。
- 不自动读取微信本地数据、不索取微信密码、不保存短信验证码。

## 7. 错误处理

| 情况 | 行为 |
|---|---|
| JWT 即将过期 | 实体仍工作，创建 Repair 提醒 |
| JWT 已过期 | 不发送控制请求，触发 Reauth |
| `401/1017/1018` | 标记鉴权失败，不自动重放，触发 Reauth |
| 网络超时 | 报告暂时不可用；用户控制动作不自动重复 |
| 签名参数无效 | 配置失败或实体不可用，不使用猜测值 |
| 本机时间明显漂移 | 报告时钟问题，不重复生成旧时间签名 |
| WSS 断开 | 有界退避重连；HTTPS 成功状态标为推定 |
| 短信验证码错误/超时 | 保留旧 JWT，不保存验证码，允许用户重新开始 Flow |

## 8. 测试与验收

测试分层：

1. 签名器：使用合成常量测试确定性、时间变化、格式校验；本机额外以私有 fixture 执行已通过的 7/7 比对。
2. 请求构造：验证开/关只有 `2.properties.1` 的布尔值变化。
3. 鉴权：覆盖 JWT 规范化、`exp` 判断、临近到期、过期和三种业务鉴权错误。
4. Config Flow：覆盖初始配置、短信重认证、手工 Token 更新、错误时不覆盖旧配置。
5. API：使用 mock transport 验证 headers、错误映射和“不自动重放”。
6. WSS：使用本地合成 MQTT 帧测试 Shadow 解析和重连状态机。
7. HA 实体：覆盖 on/off、可用性、推定状态和卸载清理。
8. 仓库：运行 Python 测试、类型/格式检查、HACS Action 和 hassfest。

默认测试必须在禁网条件下通过。真实联网验收作为独立受控步骤，需要用户再次明确授权，并且先只读取账户/设备状态；实际开关验证不得与普通单元测试混在一起。

## 9. 非目标

- 不破解微信账号体系或自动生成微信一次性登录码。
- 不自动读取短信、转发验证码或削弱二次认证。
- 不公开或硬编码小程序签名常量。
- 不支持未经抓包确认的浴霸功能。
- 不声称局域网控制或设备侧 MQTT 已可用。
- 不从本次设计审批推断 GitHub 创建、push、公开发布或真实设备控制授权。

## 10. 交付顺序

1. 建立 HACS/HA 集成骨架和完全离线的测试环境。
2. 实现签名器、JWT 生命周期和请求正文构造。
3. 实现 Config Flow、手工 JWT 持久化和 Repair 提醒。
4. 实现 `light` 实体与 mock API 验收。
5. 实现短信 Reauth Flow，但默认不进行真实接口测试。
6. 实现 WSS 状态协调和推定状态降级。
7. 运行完整离线验证及秘密扫描。
8. 经用户另行授权后进行一次受控只读认证测试，再决定是否执行一次真实照明开关验证。
9. 经用户另行授权后创建/关联 GitHub 仓库并发布 HACS 版本。
