# 奥普 Q360T5-Pro Home Assistant / HACS 集成实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 构建一个可通过 GitHub/HACS 安装的 Home Assistant 自定义集成，为 Q360T5-Pro 提供照明实体、动态应用签名、JWT 持久化、短信重新认证和 AWS IoT WSS 状态反馈。

**架构：** 集成把私有配置、认证生命周期、HTTP 控制和 MQTT-over-WSS 状态通道分离。所有云端 I/O 都经过可注入 transport，测试只使用合成 fixture；控制请求不自动重放。签名参数、JWT、设备 ID、手机号和 WSS 凭据只存在于 HA 本地配置或内存，GitHub 仓库不包含真实值。

**技术栈：** Python 3.13、Home Assistant Custom Integration API、`aiohttp`（由 HA 提供）、MQTT 3.1.1 最小编解码、pytest、pytest-homeassistant-custom-component、ruff、mypy、HACS/hassfest。

---

## 文件结构与职责

计划创建或修改：

```text
.github/workflows/validate.yml                     # pytest、ruff、mypy、HACS、hassfest
custom_components/aupu_q360/__init__.py            # Config Entry 装载、卸载、运行时对象
custom_components/aupu_q360/manifest.json          # HA 集成元数据，无生产 requirements
custom_components/aupu_q360/const.py               # domain、配置键、接口路径、错误码
custom_components/aupu_q360/models.py              # 不可变配置、状态与运行时 dataclass
custom_components/aupu_q360/signer.py              # App-Authorization 生成
custom_components/aupu_q360/auth.py                # Bearer 规范化、JWT exp 与认证状态
custom_components/aupu_q360/errors.py              # 统一且不含秘密的错误类型
custom_components/aupu_q360/api.py                 # HTTPS 请求、控制正文、短信登录
custom_components/aupu_q360/mqtt_codec.py          # MQTT 3.1.1 二进制编解码
custom_components/aupu_q360/shadow.py              # AWS IoT Shadow Topic/正文解析
custom_components/aupu_q360/wss.py                 # aiohttp WSS 生命周期与订阅
custom_components/aupu_q360/coordinator.py         # HA 状态协调、WSS 降级、Repair
custom_components/aupu_q360/config_flow.py         # 初始配置、Options、短信 Reauth
custom_components/aupu_q360/light.py               # 唯一照明实体
custom_components/aupu_q360/diagnostics.py         # 严格脱敏诊断
custom_components/aupu_q360/strings.json           # 英文 UI 文案
custom_components/aupu_q360/translations/zh-Hans.json # 简体中文文案
tests/conftest.py                                   # HA 测试 fixture 和禁网门
tests/fixtures/synthetic_signer.json                # 合成签名常量，不含真实值
tests/test_manifest.py                              # HACS/manifest/翻译一致性
tests/test_signer.py                                # 签名固定向量与失败关闭
tests/test_auth.py                                  # JWT 生命周期
tests/test_api.py                                   # 请求结构、错误与无重放
tests/test_config_flow.py                           # 初始、Options、Reauth
tests/test_light.py                                 # on/off、推定状态和可用性
tests/test_mqtt_codec.py                            # MQTT 数据包编解码
tests/test_shadow.py                                # Shadow 状态解析
tests/test_wss.py                                   # WSS 凭据、订阅、重连边界
tests/test_diagnostics.py                           # 诊断脱敏
scripts/verify_private_signer.py                    # 本机 7/7 验证，不输出秘密
scripts/check_no_secrets.py                         # 仓库秘密扫描
pyproject.toml                                      # 开发工具与检查配置
uv.lock                                             # 解析后的开发依赖锁
hacs.json                                           # HACS 集成元数据
README.md                                           # 安装、配置、重新认证和边界
```

模块边界固定如下：

- `signer.py` 不知道 HTTP、HA 或设备信息。
- `auth.py` 不发送网络请求，只判断和规范化凭据。
- `api.py` 不操作 HA 状态，只返回结构化结果或抛出分类错误。
- `wss.py` 只负责连接与 MQTT 消息，不解析业务 Shadow。
- `coordinator.py` 把 API/WSS 事件映射为 HA 状态和 Repair。
- `light.py` 不构造协议正文，只调用 coordinator 的照明方法。

## 任务 1：建立 HACS/HA 骨架和禁网测试门

**文件：**

- 创建：`custom_components/aupu_q360/__init__.py`
- 创建：`custom_components/aupu_q360/manifest.json`
- 创建：`custom_components/aupu_q360/const.py`
- 创建：`custom_components/aupu_q360/strings.json`
- 创建：`custom_components/aupu_q360/translations/zh-Hans.json`
- 创建：`hacs.json`
- 创建：`pyproject.toml`
- 创建：`tests/conftest.py`
- 创建：`tests/test_manifest.py`
- 修改：`.gitignore`

- [ ] **步骤 1：写 manifest/HACS 失败测试**

```python
def test_manifest_is_hacs_installable(project_root: Path) -> None:
    manifest = json.loads((project_root / "custom_components/aupu_q360/manifest.json").read_text())
    assert manifest["domain"] == "aupu_q360"
    assert manifest["config_flow"] is True
    assert manifest["iot_class"] == "cloud_push"
    assert manifest["requirements"] == []
    assert json.loads((project_root / "hacs.json").read_text())["name"] == "AUPU Q360"
```

- [ ] **步骤 2：运行测试并确认因文件不存在而失败**

运行：`python -m pytest tests/test_manifest.py -v`

预期：FAIL，指出 `manifest.json` 或 `hacs.json` 不存在。

- [ ] **步骤 3：建立开发环境和锁文件**

运行：

```powershell
uv init --bare
uv add --dev pytest pytest-asyncio pytest-homeassistant-custom-component ruff mypy
```

`pyproject.toml` 追加：

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]

[tool.ruff]
target-version = "py313"
line-length = 100

[tool.mypy]
python_version = "3.13"
strict = true
```

- [ ] **步骤 4：实现最小集成元数据**

`manifest.json` 使用：

```json
{
  "domain": "aupu_q360",
  "name": "AUPU Q360",
  "codeowners": [],
  "config_flow": true,
  "integration_type": "device",
  "iot_class": "cloud_push",
  "requirements": [],
  "version": "0.1.0"
}
```

远程仓库尚未获准创建，因此初始 manifest 不填写 `documentation`；用户以后授权 GitHub 发布时再用真实仓库地址单独提交。`tests/conftest.py` 使用 pytest 的 socket monkeypatch，让未显式 mock 的 DNS/TCP 调用立即失败。

- [ ] **步骤 5：运行骨架验证**

运行：

```powershell
uv run pytest tests/test_manifest.py -v
uv run ruff check custom_components tests
```

预期：全部 PASS，且测试没有网络访问。

- [ ] **步骤 6：提交骨架**

```powershell
git add pyproject.toml uv.lock hacs.json custom_components tests .gitignore
git commit -m "feat: 建立 AUPU Q360 HACS 集成骨架"
```

## 任务 2：移植并锁定动态 App-Authorization 签名器

**文件：**

- 创建：`custom_components/aupu_q360/signer.py`
- 创建：`tests/fixtures/synthetic_signer.json`
- 创建：`tests/test_signer.py`
- 创建：`scripts/verify_private_signer.py`
- 修改：`tools/app_authorization_signer.py`

- [ ] **步骤 1：生成合成固定向量并写失败测试**

合成 fixture 只使用明显虚构值，例如：

```json
{
  "app_key": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
  "key_prefix": "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
  "package_name": "com.kdyapp",
  "key_suffix": "CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC",
  "sdk_version": "1.2.3",
  "message_prefix": "appkey=",
  "sdk_label": "&sdkversion=",
  "type_timestamp_label": "&apptype=android&timestamp=",
  "header_prefix": "Synthetic: ",
  "header_sep_1": ",Sdk=",
  "header_sep_2": ",Timestamp=",
  "signature_label": ",Signature="
}
```

测试覆盖：固定时间输出、同秒确定性、相邻秒不同、负数时间拒绝、字段缺失拒绝、未知字段拒绝、秘密不出现在 `repr`。

```python
def test_signer_is_deterministic_for_fixed_timestamp(synthetic_secrets: SignerSecrets) -> None:
    signer = AppAuthorizationSigner(synthetic_secrets)
    first = signer.sign(1_700_000_000)
    second = signer.sign(1_700_000_000)
    assert first == second
    assert len(first) > 100
```

- [ ] **步骤 2：运行并确认导入失败**

运行：`uv run pytest tests/test_signer.py -v`

预期：FAIL，`custom_components.aupu_q360.signer` 尚不存在。

- [ ] **步骤 3：移植最小签名实现**

从已验证的 `tools/app_authorization_signer.py` 移植 `SignerSecrets` 和 `AppAuthorizationSigner`。生产模块不提供打印签名的 CLI；`SignerSecrets.__repr__` 返回字段名和长度，不返回值。

- [ ] **步骤 4：加入本机私有 7/7 校验脚本**

`scripts/verify_private_signer.py` 读取：

```text
.private/signer_secrets.json
local-evidence/redacted/../signer/signer-verification.safe.json
```

实际逐字节比对继续使用原临时目录内的原始 HAR。脚本输出仅包含请求数量、匹配数量、路径和布尔值；缺少私有文件时以清晰消息跳过，不能回显秘密。

- [ ] **步骤 5：运行签名测试与私有验证**

运行：

```powershell
uv run pytest tests/test_signer.py -v
uv run python scripts/verify_private_signer.py
```

预期：合成测试 PASS；本机私有验证 `exact_match_count == 7`。

- [ ] **步骤 6：提交签名器**

```powershell
git add custom_components/aupu_q360/signer.py tests/fixtures/synthetic_signer.json tests/test_signer.py scripts/verify_private_signer.py tools/app_authorization_signer.py
git commit -m "feat: 实现动态 App-Authorization 签名"
```

## 任务 3：实现 JWT 本地生命周期和分类错误

**文件：**

- 创建：`custom_components/aupu_q360/errors.py`
- 创建：`custom_components/aupu_q360/auth.py`
- 创建：`tests/test_auth.py`

- [ ] **步骤 1：写 JWT 状态失败测试**

使用测试内生成的无签名合成 JWT，不放真实 Token：

```python
def test_token_lifecycle_uses_exp_without_treating_claims_as_verified() -> None:
    token = make_synthetic_jwt({"exp": 1_700_086_400, "sub": "synthetic"})
    credential = BearerCredential.parse(token)
    assert credential.authorization_header.startswith("Bearer ")
    assert credential.expires_at == datetime.fromtimestamp(1_700_086_400, UTC)
    assert credential.state(now=datetime.fromtimestamp(1_700_000_000, UTC)) is AuthState.READY
```

另测：已有 `Bearer ` 前缀不重复、缺少 `exp`、坏 Base64、坏 JSON、24 小时内 `EXPIRING`、已过期 `EXPIRED`，以及异常消息不含输入 Token。

- [ ] **步骤 2：运行并确认类型不存在**

运行：`uv run pytest tests/test_auth.py -v`

预期：FAIL，`BearerCredential` 尚不存在。

- [ ] **步骤 3：实现最小认证模型和错误层级**

```python
class AupuError(Exception): ...
class AupuAuthError(AupuError): ...
class AupuRateLimitError(AupuError): ...
class AupuTemporaryError(AupuError): ...
class AupuProtocolError(AupuError): ...

class AuthState(StrEnum):
    READY = "ready"
    EXPIRING = "expiring"
    EXPIRED = "expired"
```

JWT 只读解析 payload 的 `exp`；不验证签名，也不将 Claims 用作权限判断。错误只包含固定 `error_code/message/retryable`，不拼接凭据。

- [ ] **步骤 4：运行认证测试和静态检查**

```powershell
uv run pytest tests/test_auth.py -v
uv run ruff check custom_components/aupu_q360/auth.py custom_components/aupu_q360/errors.py tests/test_auth.py
uv run mypy custom_components/aupu_q360/auth.py custom_components/aupu_q360/errors.py
```

预期：全部 PASS。

- [ ] **步骤 5：提交认证模型**

```powershell
git add custom_components/aupu_q360/auth.py custom_components/aupu_q360/errors.py tests/test_auth.py
git commit -m "feat: 实现 JWT 持久会话生命周期"
```

## 任务 4：实现控制正文和无重放 HTTPS 客户端

**文件：**

- 创建：`custom_components/aupu_q360/models.py`
- 创建：`custom_components/aupu_q360/api.py`
- 创建：`tests/test_api.py`
- 修改：`custom_components/aupu_q360/const.py`

- [ ] **步骤 1：写开关差异和鉴权错误失败测试**

```python
def test_light_bodies_only_differ_at_confirmed_boolean(device: DeviceConfig) -> None:
    on_body = build_light_control_body(device, is_on=True)
    off_body = build_light_control_body(device, is_on=False)
    assert on_body["did"] == 123456789
    assert on_body["sendBody"]["state"]["desired"][device.did]["2"]["properties"]["1"] is True
    assert off_body["sendBody"]["state"]["desired"][device.did]["2"]["properties"]["1"] is False
    assert json_diff(on_body, off_body) == {
        f"sendBody.state.desired.{device.did}.2.properties.1"
    }
```

异步测试用假 session 捕获一次请求，验证：动态签名、规范 Bearer、固定路径、HTTP/业务错误映射，以及超时或 5xx 时控制调用次数仍为 1。

- [ ] **步骤 2：运行并确认构造器不存在**

运行：`uv run pytest tests/test_api.py -v`

预期：FAIL，`build_light_control_body` 尚不存在。

- [ ] **步骤 3：实现不可变设备配置和 API transport**

```python
@dataclass(frozen=True, slots=True)
class DeviceConfig:
    did: str
    tag: str

    @property
    def topic_name(self) -> str:
        return f"$aws/things/{self.did}/shadow/update"
```

`AupuApiClient.request()` 每次调用 signer，使用 HA 注入的 `aiohttp.ClientSession`，解析 `status/result/timestamp`。控制方法不包含自动重试装饰器。

`DeviceConfig.did` 在内存中使用字符串，以便安全构造 Topic 和动态 JSON 键；控制请求最外层 `did` 按抓包证据序列化为整数。

- [ ] **步骤 4：运行 API 测试**

```powershell
uv run pytest tests/test_api.py -v
uv run ruff check custom_components/aupu_q360/models.py custom_components/aupu_q360/api.py tests/test_api.py
```

预期：PASS；假 transport 记录每个控制动作恰好一次 POST。

- [ ] **步骤 5：提交 API 层**

```powershell
git add custom_components/aupu_q360/const.py custom_components/aupu_q360/models.py custom_components/aupu_q360/api.py tests/test_api.py
git commit -m "feat: 实现 Q360 照明云端控制客户端"
```

## 任务 5：实现初始 Config Flow、持久化和手工 Token 更新

**文件：**

- 修改：`custom_components/aupu_q360/__init__.py`
- 创建：`custom_components/aupu_q360/config_flow.py`
- 创建：`tests/test_config_flow.py`
- 修改：`custom_components/aupu_q360/models.py`
- 修改：`custom_components/aupu_q360/strings.json`
- 修改：`custom_components/aupu_q360/translations/zh-Hans.json`

- [ ] **步骤 1：写完全本地的配置 Flow 失败测试**

测试步骤：

```python
result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
result = await hass.config_entries.flow.async_configure(
    result["flow_id"],
    {
        "signer_json": json.dumps(SYNTHETIC_SIGNER),
        "token": make_synthetic_jwt({"exp": future_exp}),
        "did": "123456789",
        "tag": "synthetic-tag",
    },
)
assert result["type"] is FlowResultType.CREATE_ENTRY
assert network_spy.call_count == 0
```

另测签名 JSON 缺字段、JWT 过期、非数字 `did`、空 `tag`、重复设备、Options 更新 Token 失败不覆盖旧值。

- [ ] **步骤 2：运行并确认 Flow 不存在**

运行：`uv run pytest tests/test_config_flow.py -v`

预期：FAIL，平台无法加载 `config_flow.py`。

- [ ] **步骤 3：实现 Config Entry 数据模型**

Config Entry 保存：`signer` 参数对象、规范 JWT、`did`、`tag`、可选 `user_uuid`，以及用户选择是否启用 WSS。唯一 ID 使用 `sha256(did.encode()).hexdigest()[:20]`，避免把设备 ID 复制到 HA registry unique_id。

初始 Flow 默认只做本地校验，不调用奥普接口。若用户启用 WSS，Flow 增加一个明确的只读连接确认页；确认后调用一次 `/authserver/auth/user/terminal/info`，只提取 `content.userUuid` 作为 AWS IoT client ID 输入。用户跳过时仍可创建仅 HTTPS 控制的 Entry，状态保持推定。Options Flow 支持替换 JWT、手机号和 WSS 开关；更新先校验后写入。

- [ ] **步骤 4：实现装载和卸载**

`async_setup_entry` 构造 signer、credential、API client 和 runtime dataclass；转发 `Platform.LIGHT`。`async_unload_entry` 停止 coordinator/WSS 并清除 runtime 引用。

- [ ] **步骤 5：运行 Flow 与装载测试**

```powershell
uv run pytest tests/test_config_flow.py -v
uv run ruff check custom_components/aupu_q360/config_flow.py custom_components/aupu_q360/__init__.py tests/test_config_flow.py
```

预期：PASS，network spy 为 0。

- [ ] **步骤 6：提交配置持久化**

```powershell
git add custom_components/aupu_q360 tests/test_config_flow.py
git commit -m "feat: 实现 HA 本地配置和 Token 持久化"
```

## 任务 6：实现 Repair、协调器和照明实体

**文件：**

- 创建：`custom_components/aupu_q360/coordinator.py`
- 创建：`custom_components/aupu_q360/light.py`
- 创建：`tests/test_light.py`
- 修改：`custom_components/aupu_q360/__init__.py`
- 修改：`custom_components/aupu_q360/models.py`
- 修改：`custom_components/aupu_q360/strings.json`
- 修改：`custom_components/aupu_q360/translations/zh-Hans.json`

- [ ] **步骤 1：写实体失败测试**

覆盖：

```python
await hass.services.async_call(LIGHT_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: entity_id}, blocking=True)
api.set_light.assert_awaited_once_with(True)
assert hass.states[entity_id].state == STATE_ON
assert hass.states[entity_id].attributes[ATTR_ASSUMED_STATE] is True
```

另测关灯传 `False`、API 失败不改变状态、过期 JWT 不调用 API、WSS 确认后 `assumed_state` 变为 false、卸载时实体和后台任务被移除。

- [ ] **步骤 2：运行并确认 light 平台不存在**

运行：`uv run pytest tests/test_light.py -v`

预期：FAIL，无法转发 `Platform.LIGHT`。

- [ ] **步骤 3：实现 coordinator 和实体**

`AupuCoordinator.async_set_light()` 先检查 credential 状态，再调用一次 API；成功后写入推定值。`AupuLight` 只公开 `async_turn_on/async_turn_off`，不注册其他浴霸服务。

- [ ] **步骤 4：实现 JWT Repair**

到期不足 24 小时创建 `jwt_expiring` issue；已过期创建不可忽略的 `jwt_expired` issue 并触发 `ConfigEntryAuthFailed`。新 Token 生效后删除对应 issue。

- [ ] **步骤 5：运行实体测试**

```powershell
uv run pytest tests/test_light.py -v
uv run ruff check custom_components/aupu_q360/coordinator.py custom_components/aupu_q360/light.py tests/test_light.py
```

预期：PASS；每个服务调用最多一个控制 POST。

- [ ] **步骤 6：提交实体和 Repair**

```powershell
git add custom_components/aupu_q360 tests/test_light.py
git commit -m "feat: 添加 Q360 照明实体和到期提醒"
```

## 任务 7：实现短信验证码 Reauth Flow

**文件：**

- 修改：`custom_components/aupu_q360/api.py`
- 修改：`custom_components/aupu_q360/config_flow.py`
- 修改：`custom_components/aupu_q360/const.py`
- 修改：`custom_components/aupu_q360/strings.json`
- 修改：`custom_components/aupu_q360/translations/zh-Hans.json`
- 修改：`tests/test_api.py`
- 修改：`tests/test_config_flow.py`

- [ ] **步骤 1：写短信 API 和 Reauth 失败测试**

API 测试验证：

```python
await api.request_sms_code(phone="13800000000")
assert captured.method == "GET"
assert captured.path.endswith("/smscode")
assert captured.params == {"areaCode": 86, "appKey": "AP", "phoneNum": "13800000000", "type": "LOG"}
```

Reauth 测试验证：发送页主动点击后才调用 API；验证码页收到 6 位码后调用登录；成功更新 Token，并从返回用户对象更新 `user_uuid`；错误或超时保留旧 Token；Config Entry 永不保存验证码。

- [ ] **步骤 2：运行并确认短信方法不存在**

运行：`uv run pytest tests/test_api.py tests/test_config_flow.py -k "sms or reauth" -v`

预期：FAIL，`request_sms_code/login_by_phone` 尚不存在。

- [ ] **步骤 3：实现短信和手机号登录 API**

`request_sms_code()` 和 `login_by_phone()` 使用现有 signer，但登录前不发送 Bearer。返回必须含非空 `token` 和带 `userUuid` 的 `user` 对象；错误响应不能进入 Config Entry。

- [ ] **步骤 4：实现 HA Reauth 状态机**

步骤名固定为：`reauth`、`reauth_method`、`reauth_sms_send`、`reauth_sms_code`、`reauth_manual_token`。手机号可选择保存在本地；验证码只保留在 flow 实例变量，并设置 5 分钟本地超时。发送按钮 60 秒内拒绝重复触发，避免滥发短信。

- [ ] **步骤 5：运行 Reauth 全测试**

```powershell
uv run pytest tests/test_api.py tests/test_config_flow.py -v
uv run ruff check custom_components/aupu_q360/api.py custom_components/aupu_q360/config_flow.py
```

预期：PASS，旧 Token 在所有失败分支保持不变。

- [ ] **步骤 6：提交短信 Reauth**

```powershell
git add custom_components/aupu_q360 tests/test_api.py tests/test_config_flow.py
git commit -m "feat: 添加短信重新认证流程"
```

## 任务 8：实现 MQTT 3.1.1 与 Shadow 纯函数层

**文件：**

- 创建：`custom_components/aupu_q360/mqtt_codec.py`
- 创建：`custom_components/aupu_q360/shadow.py`
- 创建：`tests/test_mqtt_codec.py`
- 创建：`tests/test_shadow.py`

- [ ] **步骤 1：写 MQTT 编解码失败测试**

测试固定二进制向量：CONNECT（Clean Session、Keep Alive 30）、SUBSCRIBE、QoS 0 PUBLISH、PINGREQ/PINGRESP、CONNACK、SUBACK，以及 Remaining Length 的 1/2/3 字节边界。解码器接受一个 WebSocket frame 内多个 MQTT 包和一个包跨 frame 的流式输入。

```python
def test_ping_packets_are_exact_mqtt_311_bytes() -> None:
    assert encode_pingreq() == b"\xC0\x00"
    assert decode_packets(b"\xD0\x00")[0].packet_type is PacketType.PINGRESP
```

- [ ] **步骤 2：写 Shadow 失败测试**

合成 Topic/JSON 覆盖 `get/accepted`、`update/accepted`、`reported` 优先、只有 `desired` 时标记未确认、无关 did/siid/piid 忽略、坏 JSON 返回协议错误。

- [ ] **步骤 3：运行并确认模块不存在**

运行：`uv run pytest tests/test_mqtt_codec.py tests/test_shadow.py -v`

预期：FAIL，两个模块尚不存在。

- [ ] **步骤 4：实现最小 MQTT 3.1.1 编解码**

只实现实际需要的包型：CONNECT/CONNACK、SUBSCRIBE/SUBACK、PUBLISH QoS 0、PINGREQ/PINGRESP、DISCONNECT。拒绝 QoS 1/2、超长 Remaining Length、非法 UTF-8 和畸形长度，不扩展为通用 broker 客户端。

- [ ] **步骤 5：实现 Shadow 解析**

```python
@dataclass(frozen=True, slots=True)
class LightShadowUpdate:
    is_on: bool
    confirmed: bool
    source: Literal["reported", "desired", "get_reported"]
```

`reported` 或 `get/accepted` 中的 reported 为确认态；只有 desired 时 `confirmed=False`。

- [ ] **步骤 6：运行纯函数测试和模糊边界用例**

```powershell
uv run pytest tests/test_mqtt_codec.py tests/test_shadow.py -v
uv run ruff check custom_components/aupu_q360/mqtt_codec.py custom_components/aupu_q360/shadow.py
uv run mypy custom_components/aupu_q360/mqtt_codec.py custom_components/aupu_q360/shadow.py
```

预期：全部 PASS。

- [ ] **步骤 7：提交协议层**

```powershell
git add custom_components/aupu_q360/mqtt_codec.py custom_components/aupu_q360/shadow.py tests/test_mqtt_codec.py tests/test_shadow.py
git commit -m "feat: 实现 AWS IoT Shadow MQTT 协议层"
```

## 任务 9：实现 AWS IoT WSS 生命周期和状态协调

**文件：**

- 创建：`custom_components/aupu_q360/wss.py`
- 创建：`tests/test_wss.py`
- 修改：`custom_components/aupu_q360/api.py`
- 修改：`custom_components/aupu_q360/coordinator.py`
- 修改：`custom_components/aupu_q360/models.py`
- 修改：`custom_components/aupu_q360/light.py`

- [ ] **步骤 1：写 WSS 连接失败测试**

假 WebSocket 记录：

- `getWssToken` 每次新连接调用一次；其结果只进入 URL 查询参数。
- `ws_connect` 使用 `protocols=("mqtt",)`。
- CONNECT 后等待 CONNACK，再订阅两个 Shadow accepted Topic。
- SUBACK 后发布一次 `shadow/get`。
- PING 间隔 30 秒；收到 PINGRESP 更新健康状态。
- 正常关闭发送 DISCONNECT。
- URL、查询参数和 token 不出现在日志或异常文本。

- [ ] **步骤 2：写重连边界失败测试**

使用可注入 clock/sleep，验证退避序列 `2, 4, 8, 16, 30, 30` 秒；鉴权错误不继续重连而通知 coordinator；卸载取消 sleep 和 receive task。

- [ ] **步骤 3：运行并确认 WSS 客户端不存在**

运行：`uv run pytest tests/test_wss.py -v`

预期：FAIL，`AupuShadowWebSocket` 尚不存在。

- [ ] **步骤 4：实现 aiohttp WSS 客户端**

通过 HA 共享 `ClientSession.ws_connect()` 建立二进制 WebSocket，子协议 `mqtt`。为复现已确认的小程序行为，client ID 由本地 `user_uuid`、JWT 尾部 8 字符和当前毫秒组成；完整 client ID 不写入 Config Entry 或日志。缺少 `user_uuid` 时不连接 WSS，实体保持推定状态并提示完成只读验证或短信 Reauth。WSS 查询参数对象用局部变量保存，连接后立即丢弃。

- [ ] **步骤 5：连接 coordinator 和实体状态**

WSS 收到 `reported` 时更新 `is_on` 且 `assumed_state=False`；只有 desired 时保留 `assumed_state=True`。断线不把最后确认状态改成相反值，只更新连接可用性。

- [ ] **步骤 6：运行 WSS、实体和卸载测试**

```powershell
uv run pytest tests/test_wss.py tests/test_light.py -v
uv run ruff check custom_components/aupu_q360/wss.py custom_components/aupu_q360/coordinator.py tests/test_wss.py
```

预期：PASS，无真实 DNS/TCP 调用，无遗留 asyncio task。

- [ ] **步骤 7：提交 WSS 状态通道**

```powershell
git add custom_components/aupu_q360 tests/test_wss.py tests/test_light.py
git commit -m "feat: 添加 AWS IoT WSS 状态反馈"
```

## 任务 10：实现严格脱敏诊断和仓库秘密扫描

**文件：**

- 创建：`custom_components/aupu_q360/diagnostics.py`
- 创建：`tests/test_diagnostics.py`
- 创建：`scripts/check_no_secrets.py`
- 修改：`tests/conftest.py`
- 修改：`.gitignore`

- [ ] **步骤 1：写诊断泄漏失败测试**

构造含合成 JWT、手机号、did、tag、签名材料和 WSS 查询参数的 runtime，调用 diagnostics 后递归序列化并断言这些值均不出现。允许字段只有：版本、连接布尔值、到期时间桶、最近错误码、状态来源和推定标志。

- [ ] **步骤 2：运行并确认 diagnostics 不存在**

运行：`uv run pytest tests/test_diagnostics.py -v`

预期：FAIL，诊断平台尚不存在。

- [ ] **步骤 3：实现白名单式诊断**

不要先复制 runtime 再删字段；从空字典只加入允许的标量。到期信息输出 `expired/<24h/<7d/>=7d/unknown`，不输出精确 `exp`。

- [ ] **步骤 4：实现仓库扫描脚本**

`scripts/check_no_secrets.py`：

1. 确认 `.private/`、`local-evidence/` 和抓包/证书扩展名被忽略。
2. 读取 `.private/signer_secrets.json` 时只在内存比较真实值是否进入 tracked files。
3. 从原始 HAR 收集的候选值只在内存比较。
4. 扫描 JWT、Bearer 长值、手机号、私钥头和完整 App-Authorization 模式。
5. 只输出命中类型、文件和计数，不输出命中值。

- [ ] **步骤 5：运行诊断和秘密扫描**

```powershell
uv run pytest tests/test_diagnostics.py -v
uv run python scripts/check_no_secrets.py
```

预期：PASS，`sensitive_hit_count = 0`。

- [ ] **步骤 6：提交诊断安全层**

```powershell
git add custom_components/aupu_q360/diagnostics.py tests/test_diagnostics.py scripts/check_no_secrets.py .gitignore tests/conftest.py
git commit -m "feat: 添加脱敏诊断和秘密扫描"
```

## 任务 11：完成文档、CI 和 HACS 离线验收

**文件：**

- 创建：`.github/workflows/validate.yml`
- 修改：`README.md`
- 修改：`hacs.json`
- 修改：`custom_components/aupu_q360/manifest.json`
- 修改：`custom_components/aupu_q360/strings.json`
- 修改：`custom_components/aupu_q360/translations/zh-Hans.json`
- 修改：`docs/research/q360t5-ha-analysis-redacted.md`
- 测试：全部 `tests/`

- [ ] **步骤 1：写元数据和文档一致性失败测试**

扩展 `tests/test_manifest.py`：manifest 版本、HACS 名称、domain、strings/中文翻译 step/error/abort 键集合必须一致；README 不得出现真实值或声称自动刷新 JWT。

- [ ] **步骤 2：运行并确认当前文档/翻译缺项失败**

运行：`uv run pytest tests/test_manifest.py -v`

预期：FAIL，指出尚未补全的翻译或 README 章节。

- [ ] **步骤 3：完成 README**

必须包含：HACS 自定义仓库安装、重启 HA、添加集成、私有签名 JSON 本地导入、JWT 到期含义、短信 Reauth、手工 Token 保底、WSS 状态、备份风险、卸载、故障排查、明确不支持其他浴霸功能。

- [ ] **步骤 4：添加 CI**

`validate.yml` 包含四个独立 job：

```yaml
- run: uv run pytest
- run: uv run ruff check .
- run: uv run mypy custom_components/aupu_q360
- uses: hacs/action@main
- uses: home-assistant/actions/hassfest@master
```

CI 不上传 `.private/`、`local-evidence/` 或测试日志 artifact。

- [ ] **步骤 5：运行完整离线验收矩阵**

```powershell
uv sync --locked
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy custom_components/aupu_q360
uv run python scripts/verify_private_signer.py
uv run python scripts/check_no_secrets.py
git diff --check
git status --short
```

预期：所有检查 PASS；私有签名 `7/7`；秘密命中 `0`；除本任务预期文件外没有未提交变更。

- [ ] **步骤 6：审查缓存差异并提交完整交付**

```powershell
git add .github README.md hacs.json custom_components docs tests pyproject.toml uv.lock scripts
git diff --cached --check
git diff --cached --stat
git commit -m "docs: 完成 AUPU Q360 HACS 安装与运维说明"
```

- [ ] **步骤 7：记录后续显式授权门**

最终报告明确以下操作尚未执行：真实短信发送、手机号登录、真实照明控制、GitHub 远程仓库创建、push、Release 和 HACS 实机安装。每项都需要用户之后单独授权；当前任务以离线可安装代码和测试为完成标准。

## 规格覆盖自检

| 规格要求 | 对应任务 |
|---|---|
| HACS/GitHub 安装结构 | 任务 1、11 |
| 动态 App-Authorization 和 7/7 验证 | 任务 2 |
| JWT 重启持久化、exp、Repair | 任务 3、5、6 |
| 控制正文唯一布尔变化、不自动重放 | 任务 4、6 |
| 初始本地配置与手工 Token 保底 | 任务 5 |
| 手机短信 Reauth，验证码不保存 | 任务 7 |
| MQTT 3.1.1 over WSS 和 Shadow | 任务 8、9 |
| WSS 失败时推定状态 | 任务 6、9 |
| 严格诊断脱敏和秘密扫描 | 任务 10 |
| 禁网测试、CI、HACS/hassfest | 任务 1、11 |
| 不支持其他浴霸功能 | 任务 6、11 |
| Token Broker 与局域网方案不进入首版 | 设计规格第 3 节，任务 11 文档说明 |

类型和方法名称在各任务间固定为：`SignerSecrets`、`AppAuthorizationSigner`、`BearerCredential`、`AuthState`、`DeviceConfig`、`AupuApiClient`、`AupuCoordinator`、`AupuShadowWebSocket`、`LightShadowUpdate`。生产代码不允许使用另一组兼容别名。
