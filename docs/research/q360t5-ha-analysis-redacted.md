# 奥普 Q360T5-Pro 云端控制与 Home Assistant 接入分析（脱敏）

生成日期：2026-09-01（Asia/Shanghai）

## 一、结论

照明开关不是由微信小程序直接在局域网内控制，而是通过奥普云端普通 HTTPS 接口提交 AWS IoT Device Shadow 更新：

- 请求：`POST https://cn-north-1-prod.aupu.net/appapi/iot/control`
- 请求类型：`application/json`
- 开灯和关灯使用完全相同的域名、路径、方法、设备、Topic 和鉴权字段。
- 两次请求正文唯一变化是：
  - 开灯：`sendBody.state.desired.{thing-id}.2.properties.1 = true`
  - 关灯：`sendBody.state.desired.{thing-id}.2.properties.1 = false`
- 两次请求均返回 HTTP `200`，应用层 `status = 0`，`result = null`；响应中只有 `timestamp` 随请求变化。

小程序同时建立 AWS IoT 的 MQTT 3.1.1 over WebSocket 连接，用于读取 Device Shadow 和接收状态更新。物理浴霸此前被路由器观察到保持至 AWS 中国北京区 `8883/TLS` 的长连接，这与本次解密证据吻合。

技术上可以为 Home Assistant 编写自定义集成。`App-Authorization` 生成算法已经从本机小程序包中静态恢复，并对 A/B/C 三阶段共 7 个请求逐字节离线验证，结果为 `7/7` 完全一致。现在可以在 HA 中按当前 Unix 秒动态生成该请求头，不需要运行微信或进行运行时注入。剩余的长期运行阻塞是 Bearer JWT 的安全登录/刷新流程；抓包中的现有 JWT 约十天后过期，不能作为长期方案。

## 二、采集证据

| 阶段 | 本地时间 | 主要有效记录 |
|---|---:|---|
| A：重新打开后静置 | 约 23:22:08 | 获取 WSS 凭据、连接 AWS IoT、订阅 Topic、读取初始 Shadow |
| B：只开灯一次 | 23:30:09 | 一次 `POST /appapi/iot/control` |
| C：只关灯一次 | 23:34:14 | 一次 `POST /appapi/iot/control` |

抓包中另有 `localhost /ping`、微信图片/遥测，以及一条与本任务无关的 Codex/ChatGPT 请求；这些均已排除，不参与结论。

## 三、控制接口

### 3.1 请求

```text
POST https://cn-north-1-prod.aupu.net/appapi/iot/control
Content-Type: application/json
Authorization: Bearer <redacted-jwt>
App-Authorization: <redacted-dynamic-app-signature>
```

脱敏后的字段结构如下：

```json
{
  "did": "<9-digit-device-id>",
  "tag": "<stable-opaque-string-length-13>",
  "topicName": "$aws/things/{thing-id}/shadow/update",
  "sendBody": {
    "state": {
      "desired": {
        "{thing-id}": {
          "2": {
            "properties": {
              "1": true
            }
          }
        }
      }
    }
  }
}
```

确认关系：`did`、Topic 中的 `{thing-id}`、`desired` 下的动态对象键三者是同一个 9 位标识；报告不保留其真实值。

开灯与关灯对比：

| 字段 | 开灯 | 关灯 |
|---|---:|---:|
| `sendBody.state.desired.{thing-id}.2.properties.1` | `true` | `false` |

其余四个顶层字段及所有请求头名称相同。`did`、`tag`、`topicName` 都没有变化。

### 3.2 响应

两次响应结构相同：

```json
{
  "version": "integer",
  "status": "integer (observed: 0)",
  "msg": "string",
  "timestamp": "integer",
  "result": null
}
```

HTTP 状态均为 `200`。除服务器 `timestamp` 外，开灯与关灯响应字段没有变化。

## 四、MQTT over WebSocket 状态通道

### 4.1 WSS 凭据获取

```text
POST https://cn-north-1-prod.aupu.net/iotservice/api/iot/wss/getWssToken
Content-Type: application/json
Body: {}
```

该接口同样要求 `Authorization` 和 `App-Authorization`。响应结构：

```json
{
  "version": "integer",
  "status": "integer",
  "msg": "string",
  "timestamp": "integer",
  "result": {
    "x-amz-customauthorizer-name": "<redacted>",
    "x-amz-customauthorizer-signature": "<redacted>",
    "tokenKeyName": "<redacted>"
  },
  "ok": "boolean"
}
```

响应中的三个值被原样用于下一步 WSS 查询参数；签名经过 URL 解码后完全一致。

### 4.2 WSS/MQTT 连接

```text
wss://aii5h05kuofsj.ats.iot.cn-north-1.amazonaws.com.cn/mqtt
```

查询参数名称：

- `x-amz-customauthorizer-name`
- `x-amz-customauthorizer-signature`
- `tokenKeyName`

握手返回 HTTP `101`，`Sec-WebSocket-Protocol` 为 `mqtt`。MQTT 证据：

- 协议名：MQTT
- 协议级别：4（MQTT 3.1.1）
- Clean Session：`true`
- Keep Alive：30 秒
- CONNECT 不携带 MQTT username/password；鉴权发生在 WSS 查询参数的 AWS IoT Custom Authorizer。
- QoS：捕获的订阅和发布均为 `0`。

捕获到的订阅 Topic：

```text
$aws/events/presence/connected/{client-id}
$aws/events/presence/disconnected/{client-id}
$aws/things/{thing-id}/shadow/update/accepted
$aws/things/{thing-id}/shadow/get/accepted
```

小程序打开后主动执行：

```text
PUBLISH $aws/things/{thing-id}/shadow/get  payload={}
```

云端随后返回：

```text
PUBLISH $aws/things/{thing-id}/shadow/get/accepted
```

返回的 Shadow 同时含 `state.desired`、`state.reported` 和 `state.delta`。A 阶段初始快照里：

```text
state.desired.{thing-id}.2.properties.1  = false
state.reported.{thing-id}.2.properties.1 = false
```

这与 B 阶段从关闭状态执行开灯、并把同一字段改为 `true` 相互印证。

## 五、鉴权与时效

### 5.1 Bearer JWT

- `Authorization` 使用 Bearer JWT。
- JWT 算法字段为 `HS512`。
- Claim 名称包括 `auth`、`exp`、`jti`、`sub`；不输出 Claim 值。
- A、B、C 所有奥普接口共用同一个 JWT。
- B 控制时距离 `exp` 约剩 `889150` 秒，约 10.3 天。

结论：把当前 JWT 固化到 HA 最多只能短期工作，而且会把账号控制权限暴露给 HA 主机；长期集成必须具备安全的刷新或重新登录流程。

### 5.2 App-Authorization

- 这是一个 159 字符的非 JWT 应用级签名。
- 签名输入只包含应用标识、SDK 版本、固定客户端类型 `android` 和当前 Unix 秒；不绑定 URL、HTTP 方法或请求正文。
- 核心摘要为 `HMAC-SHA256`，其十六进制文本再做 UTF-8 Base64 编码，最后与应用信息、时间戳和签名标签拼接成请求头。
- HMAC 密钥由小程序包内三段固定文本拼接而成；真实常量只保存在本机私有 `signer_secrets.json`，本报告不包含其值。
- A 阶段同一秒的 5 个不同 API 请求共用同一签名；B、C 使用各自请求秒的签名。
- 使用每条捕获头部中的时间戳重新生成后，A/B/C 共 7 条奥普请求全部逐字节匹配，`exact_match_count = 7/7`。
- 控制 JSON 中没有显式 `timestamp`、`nonce`、`signature` 或一次性 Token 字段。

结论：算法已被静态恢复并完成离线证实，可在 HA 中独立实现。它不是绑定正文的一次性签名；HA 主机时钟必须准确。奥普仍可能在未来的小程序或服务端升级中更换嵌入常量或格式。

### 5.3 AWS IoT Custom Authorizer

- WSS 查询中明确存在 Custom Authorizer 名称、签名和 `tokenKeyName`。
- 这些值由奥普 `getWssToken` 接口动态下发。
- 捕获中没有明确的 WSS Token 过期字段，不能断言其寿命或能否复用。

## 六、Home Assistant 接入判断

### 方案 A：调用奥普 HTTPS 控制接口

可行性：中等，协议正文已完全明确。

优点：

- 开/关只差一个布尔值。
- 服务器已对两种请求返回成功状态。
- 不需要直接持有设备 MQTT 客户端证书。

阻塞与风险：

- Bearer JWT 会过期。
- `App-Authorization` 已可本地生成，但它依赖从小程序包恢复的私有常量；常量不得写入公开仓库、日志或 Issue。
- 云端接口不是公开文档 API，字段或签名算法可能随小程序升级而变化。
- 凭据泄露后可控制账号下的设备，严禁写入日志、Git、Issue 或聊天。

### 方案 B：HA 直连 AWS IoT WSS

可行性：中低。

状态读取协议和 Topic 已明确，但仍要先调用奥普的 `getWssToken`，因此没有绕开奥普鉴权。AWS IoT 策略还可能绑定 Client ID 或限制可发布 Topic，不能假设任意 HA MQTT 客户端都能直接使用。

### 方案 C：直连设备的 `8883` MQTT

可行性：低。

路由器只能证明设备使用 TLS MQTT，未获得设备侧客户端证书或私钥。小程序采用的是 WSS Custom Authorizer，不是设备的 `8883` 证书。当前没有证据表明 HA 可以用用户名/密码直连该端口。

### 方案 D：局域网控制

可行性：未知。

曾观察到设备局域网 IP，但本次小程序抓包没有任何面向该 IP 的请求，不能据此宣称 Q360T5-Pro 支持可用的本地控制协议。

## 七、建议的下一阶段

另一台电脑现在可以创建不联网的 Home Assistant 自定义集成，并接入已验证的签名器：

1. 将不含常量的 `app_authorization_signer.py` 作为签名模块；它从外部私有 JSON 加载常量，并以当前 Unix 秒生成请求头。
2. 创建一个 `light` 实体，仅映射 `true/false` 字段。
3. 创建云端客户端模块，参数包括 `did/thing-id`、`tag`、Bearer 提供器和 `App-Authorization` 签名器。
4. 创建 JWT 凭据提供器；先只支持从 HA 私有配置读取现有 JWT，并明确报告到期时间，随后再恢复安全的登录/刷新流程。
5. 可选创建状态协调器，通过 AWS IoT WSS 订阅 `shadow/update/accepted`，并通过 `shadow/get` 初始化状态；WSS 凭据每次从 `getWssToken` 获取。
6. 测试全部基于本地 fixture，不允许真实重放，直到用户另行明确授权一次受控验证。
7. 正式联网前仍需解决 JWT 的登录/刷新流程，并验证 WSS Custom Authorizer 凭据的有效期和刷新条件。

本机已对微信电脑版 `V1MMWX` 小程序主包和分包完成本地解密、格式校验与静态解包。签名模块来自主包 Webpack 模块，算法和常量提取均在本地完成。静态结果已足够，不需要 Frida、进程注入、Root、越狱或微信运行时绕过。

## 八、交付与清理状态

- Reqable 已退出。
- `0.0.0.0:9000` 已停止监听。
- Windows 系统代理已恢复为 Mihomo：`127.0.0.1:7897`。
- 已生成三份本地脱敏 HAR；原始 HAR 未上传、未放入 Git。
- 已生成不含硬编码常量的签名模块和离线验证脚本；私有常量文件只保存在本机受限临时目录。
- Reqable 当前用户 CA、原始 HAR 和临时目录仍保留；删除前必须再次征得用户确认。

本报告和不含秘密的签名源码可以复制给另一台电脑的 Codex。HA 实际运行还需要私有常量、设备参数和合法 JWT；不要通过聊天、Git、Issue 或公开云盘传输它们。建议由用户通过本地加密压缩包或离线介质直接复制到另一台受信任电脑，并放入 HA 的私有配置目录。不要复制原始 HAR、Cookie 或 WSS 签名。

## 九、脱敏实现与离线验收结论

自定义集成现已实现本地配置、JWT 到期 Repair、短信或手工 Token 重新认证、单一照明实体、
HTTPS 单次控制、可选 WSS 状态反馈、标准诊断和卸载清理。公开代码与测试只使用合成凭据和
fake transport；没有执行真实短信、登录、照明控制或 AWS I/O。

任务 11 提交前的本地验证状态如下：

- Windows default 离线套件为 `233/233`；Ruff check、Ruff format check 与生产模块 mypy 通过。
- 私有签名器复核只记录安全计数 `7/7`，不记录候选值或原始请求。
- tracked-file 秘密扫描覆盖 44 个文件，私有比对源可用，敏感命中为 0。
- Linux-only HA runtime 测试源码已覆盖真实 flow/config-entry/entity service/Repair/unload/
  diagnostics manager 边界以及 fake WSS 生命周期；受 Windows `fcntl` 边界限制，本机仅完成
  语法与静态检查，没有执行这些测试，也不把它们报告为 PASS。

剩余风险与发布门保持不变：尚无已确认的 GitHub owner/repository URL，也未创建远程仓库、
push、Release、部署或执行 HACS 实机安装。HACS/hassfest 的远程结果与 Linux HA runtime
结果只能由后续获授权的首次 GitHub Actions 实证。真实短信发送、手机号登录和照明控制也都
需要用户另行逐项授权；厂商仍可能更新私有云 API、签名格式或 WSS 策略。
