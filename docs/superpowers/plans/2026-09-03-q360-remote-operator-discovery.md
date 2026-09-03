# Q360 远程操作者状态发现 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 Q360 v2 真实发现从“操作者必须在实体面板旁”升级为“手机官方控制面操作、现场人员异常兜底”，同时保持 discovery 协议和证据模型不变。

**Architecture:** 只修改用户可见提示、运行文档和补丁版本；五个 Actions、状态机、实验目录、Shadow `get`、schema 2 报告和 v2 Store key 都不变。手机上的奥普官方 App 或官方微信小程序是独立人工控制链，字段证据仍只来自目标设备 Shadow `reported`。

**Tech Stack:** Home Assistant custom integration、Python `>=3.13.2`、JSON translations、YAML Action schema、Markdown、pytest、Ruff、mypy、uv。

**Spec:** `docs/superpowers/specs/2026-09-03-q360-remote-operator-discovery-design.md`

## Global Constraints

- 基线提交为 `3494127`；执行前必须重新核对 HEAD、工作树和适用的 `AGENTS.md`。
- 不新增生产或开发依赖；只使用项目现有 `uv` 环境和已锁定依赖。
- discovery 只能通过现有 `async_request_shadow_get` 发送关联 Shadow `get`；不得增加 Shadow `update`、`desired`、HTTP 控制或官方控制面私有 API。
- 官方 App/微信小程序只承担人工操作和安全流程，显示内容不得进入字段候选、Store、Diagnostics 或报告。
- 不修改 `custom_components/aupu_q360/services.yaml`、Action 名称/参数、固定 message codes、状态机、实验目录、报告 schema、Store key、WSS 或档案实现。
- 原始 topic、payload、Token、Cookie、签名材料、设备标识、Config Entry 内容和档案内容不得进入终端输出、测试夹具、Git、聊天或文档。
- `.codegraph/` 是本地索引；不得编辑、暂存、提交、删除或部署。
- 三个任务分别形成一个本地提交。执行前需取得覆盖这三个明确提交的授权，或在每次提交前分别授权；提交授权不包含推送。
- 推送、同步组件、重启 HA、启用私有档案和真实设备实验仍是五个相互独立的后续授权门。

---

### Task 1: 让 HA Action 提示支持远程操作者控制面

**Files:**

- Modify: `tests/test_manifest.py:166-260`
- Modify: `custom_components/aupu_q360/strings.json:103-209`
- Modify: `custom_components/aupu_q360/translations/zh-Hans.json:103-209`

**Interfaces:**

- Consumes: 现有五个 Action key、二十七个 discovery exception/message key、`_scalar_strings(value: object) -> list[str]` 和 `_key_tree(value: object) -> object`。
- Produces: 完全相同的 JSON key tree 和 Action schema；英文统一使用 `operator control surface`，中文统一使用“操作者控制面”，远程恢复失败明确要求取消并安排现场检查。

- [ ] **Step 1: 写入失败的提示契约测试**

在 `test_discovery_actions_have_fixed_ui_schemas_and_translations` 的 key-tree 断言后加入：

```python
    english_services = " ".join(_scalar_strings(strings["services"]))
    english_exceptions = " ".join(_scalar_strings(strings["exceptions"]))
    chinese_services = " ".join(_scalar_strings(translation["services"]))
    chinese_exceptions = " ".join(_scalar_strings(translation["exceptions"]))

    assert (
        "operator control surface (physical panel, official AUPU app, or official WeChat "
        "mini program)"
        in english_services
    )
    assert "操作者控制面（实体面板、奥普官方 App 或官方微信小程序）" in chinese_services
    assert "remote sessions must cancel and arrange an on-site inspection" in english_exceptions
    assert "远程会话必须取消并安排现场检查" in chinese_exceptions

    for obsolete_panel_only_copy in (
        "on the physical panel",
        "inspect the physical panel",
    ):
        assert obsolete_panel_only_copy not in english_services
        assert obsolete_panel_only_copy not in english_exceptions
    for obsolete_panel_only_copy in (
        "请在实体面板",
        "检查实体面板",
    ):
        assert obsolete_panel_only_copy not in chinese_services
        assert obsolete_panel_only_copy not in chinese_exceptions

    prompt_keys = {
        "discovery_prompt_mode_on",
        "discovery_prompt_mode_restore",
        "discovery_prompt_carrier_on",
        "discovery_prompt_parameter_change",
        "discovery_prompt_parameter_restore",
        "discovery_prompt_carrier_off",
        "discovery_prompt_idle_observation",
    }
    for key in prompt_keys:
        assert "operator control surface" in strings["exceptions"][key]["message"]
        assert "操作者控制面" in translation["exceptions"][key]["message"]
```

- [ ] **Step 2: 运行测试并确认按预期失败**

Run:

```bash
uv run pytest tests/test_manifest.py::test_discovery_actions_have_fixed_ui_schemas_and_translations -v
```

Expected: FAIL at the first new assertion because the current copy only names the physical panel and has no official remote control surface or on-site fallback wording.

- [ ] **Step 3: 最小更新英文源字符串**

在 `strings.json` 保持所有 key 不变，使用下面的精确文案替换对应值：

```json
{
  "start_discovery.description": "Starts reported-only Shadow discovery after all seven modes are confirmed off on an operator control surface.",
  "start_discovery.fields.all_modes_off_confirmed.description": "Confirm on an operator control surface (physical panel, official AUPU app, or official WeChat mini program) that all seven modes are off before the baseline snapshot.",
  "begin_discovery_step.fields.source_level.description": "Required only for the global fan-level experiment; restore this value on the operator control surface before carrier shutdown.",
  "begin_discovery_step.fields.source_temperature.description": "Required only for the AI target-temperature experiment; restore this value on the operator control surface before carrier shutdown.",
  "cancel_discovery.description": "Stops software collection without sending restoration commands; remote sessions require an on-site inspection when prompted.",
  "discovery_wss_unavailable": "The read-only state channel is unavailable. Stop collection, restore through the operator control surface, and arrange an on-site inspection.",
  "discovery_snapshot_timeout": "The correlated Shadow snapshot timed out. Stop collection, restore through the operator control surface, and arrange an on-site inspection.",
  "discovery_step_expired": "The active discovery stage expired. Stop collection, restore through the operator control surface, and arrange an on-site inspection.",
  "discovery_session_expired": "The read-only discovery session expired. Stop collection, restore through the operator control surface, and arrange an on-site inspection.",
  "discovery_resource_limit": "The active stage exceeded its sanitized change limit. Stop collection, restore through the operator control surface, and arrange an on-site inspection.",
  "discovery_raw_archive_failed": "The private raw archive failed. Stop collection, restore through the operator control surface, and arrange an on-site inspection.",
  "discovery_raw_archive_limit": "The private raw archive reached its fixed limit. Stop collection, restore through the operator control surface, and arrange an on-site inspection.",
  "discovery_restore_required": "Reported restoration is not confirmed. Local operators may restore and advance again; remote sessions must cancel and arrange an on-site inspection.",
  "discovery_manual_restore_required": "Collection stopped before restoration was confirmed. Restore all modes, the original level, and the original temperature through the operator control surface, then arrange an on-site inspection.",
  "discovery_prompt_mode_on": "Turn on only the selected mode on the operator control surface, refresh it, then advance.",
  "discovery_prompt_mode_restore": "Turn the selected mode off on the operator control surface, refresh it to confirm, then advance.",
  "discovery_prompt_carrier_on": "Turn on the catalog carrier mode on the operator control surface, refresh it, then advance.",
  "discovery_prompt_parameter_change": "Change the tested level or temperature from the original value to the target value on the operator control surface, refresh it, then advance.",
  "discovery_prompt_parameter_restore": "Restore the original level or temperature on the operator control surface, refresh it, then advance.",
  "discovery_prompt_carrier_off": "Turn the carrier mode off on the operator control surface, refresh it to confirm, then advance.",
  "discovery_prompt_idle_observation": "Do not use the operator control surface during the idle observation, then advance."
}
```

这里的点分隔名称只是定位现有嵌套 key，不得把 JSON 文件改成扁平结构。其他 service 和
exception 文案保持原值。

- [ ] **Step 4: 同步简体中文翻译**

在 `zh-Hans.json` 保持与 `strings.json` 完全相同的 key tree，使用下面的精确文案替换对应值：

```json
{
  "start_discovery.description": "在操作者控制面确认七个模式均已关闭后，开始仅使用 reported 的 Shadow 发现。",
  "start_discovery.fields.all_modes_off_confirmed.description": "获取基线快照前，在操作者控制面（实体面板、奥普官方 App 或官方微信小程序）确认七个模式均已关闭。",
  "begin_discovery_step.fields.source_level.description": "仅全局档位实验必填；关闭载体前必须在操作者控制面恢复此值。",
  "begin_discovery_step.fields.source_temperature.description": "仅 AI 目标温度实验必填；关闭载体前必须在操作者控制面恢复此值。",
  "cancel_discovery.description": "停止软件采集且不发送恢复命令；远程会话收到提示时必须安排现场检查。",
  "discovery_wss_unavailable": "只读状态通道不可用。请停止采集，通过操作者控制面恢复，并安排现场检查。",
  "discovery_snapshot_timeout": "关联 Shadow 快照等待超时。请停止采集，通过操作者控制面恢复，并安排现场检查。",
  "discovery_step_expired": "当前发现阶段已超时。请停止采集，通过操作者控制面恢复，并安排现场检查。",
  "discovery_session_expired": "只读发现会话已超时。请停止采集，通过操作者控制面恢复，并安排现场检查。",
  "discovery_resource_limit": "当前阶段超过脱敏变化上限。请停止采集，通过操作者控制面恢复，并安排现场检查。",
  "discovery_raw_archive_failed": "私有原始档案写入失败。请停止采集，通过操作者控制面恢复，并安排现场检查。",
  "discovery_raw_archive_limit": "私有原始档案达到固定上限。请停止采集，通过操作者控制面恢复，并安排现场检查。",
  "discovery_restore_required": "reported 尚未确认恢复。现场操作者可恢复后重试同一阶段；远程会话必须取消并安排现场检查。",
  "discovery_manual_restore_required": "停止采集前尚未确认恢复。请通过操作者控制面恢复所有模式、原档位和原温度，然后安排现场检查。",
  "discovery_prompt_mode_on": "请只在操作者控制面开启所选模式，刷新后推进。",
  "discovery_prompt_mode_restore": "请在操作者控制面关闭所选模式，刷新确认后推进。",
  "discovery_prompt_carrier_on": "请在操作者控制面开启目录指定的载体模式，刷新后推进。",
  "discovery_prompt_parameter_change": "请在操作者控制面把所测档位或温度从原值改为目标值，刷新后推进。",
  "discovery_prompt_parameter_restore": "请在操作者控制面恢复原档位或原温度，刷新后推进。",
  "discovery_prompt_carrier_off": "请在操作者控制面关闭载体模式，刷新确认后推进。",
  "discovery_prompt_idle_observation": "空闲观察期间不要操作操作者控制面，等待后推进。"
}
```

- [ ] **Step 5: 运行聚焦测试和格式检查**

Run:

```bash
uv run pytest tests/test_manifest.py::test_discovery_actions_have_fixed_ui_schemas_and_translations -v
uv run ruff check tests/test_manifest.py
uv run ruff format --check tests/test_manifest.py
git diff --check
```

Expected: all commands PASS; `services.yaml` is byte-identical to HEAD and the English/Chinese key trees still match.

- [ ] **Step 6: 检查并提交 Task 1**

Run:

```bash
git diff -- custom_components/aupu_q360/strings.json custom_components/aupu_q360/translations/zh-Hans.json tests/test_manifest.py
git diff --exit-code -- custom_components/aupu_q360/services.yaml
git status --short
```

Expected: only the two JSON files and `tests/test_manifest.py` are modified; the plan file may remain untracked and must not be staged with this task.

After the task's commit authorization is in force:

```bash
git add custom_components/aupu_q360/strings.json custom_components/aupu_q360/translations/zh-Hans.json tests/test_manifest.py
git commit -m "fix(状态发现): 支持远程操作者提示"
```

---

### Task 2: 把 README 和运行手册改为纯手机远程实验

**Files:**

- Modify: `tests/test_manifest.py:263-326`
- Modify: `README.md:70-97`
- Modify: `docs/q360-read-only-discovery-runbook.md:1-200`

**Interfaces:**

- Consumes: Task 1 的“操作者控制面”术语、现有十个实验标签和五个 Action 名称。
- Produces: 可直接执行的纯手机远程运行流程；官方控制面负责人工操作，HA 负责 discovery Actions，现场人员只在异常或恢复不确定时强制接管。

- [ ] **Step 1: 写入失败的远程文档契约测试**

将测试函数改名为 `test_v2_discovery_docs_match_the_remote_operator_contract`。保留现有 Action、
只读边界、实验目录和私有档案断言，并在读取文档后加入：

```python
    for document in (readme, runbook):
        for required_remote_copy in (
            "操作者控制面",
            "奥普官方 App",
            "官方微信小程序",
            "只有目标设备 Shadow `reported`",
        ):
            assert required_remote_copy in document

    assert "操作者位于实体面板旁" not in runbook
    assert "操作者在实体面板上完成" not in runbook

    compact_runbook = re.sub(r"\s+", "", runbook)
    for required_remote_boundary in (
        "连续45–60分钟",
        "浴室无人、无宠物",
        "可能控制Q360的自动化已暂停",
        "手机、电脑HA和聊天在线",
        "官方控制面显示只用于核对人工操作，不能作为字段证据",
        "discovery_restore_required",
        "远程会话不得利用同阶段重试能力",
        "无论官方控制面显示什么",
        "家中人员现场确认",
        "当前载体仍开启时，先恢复原档位或原温度，再关闭全部模式",
        "恢复需要重新开启模式时，不再远程尝试，由现场人员接管",
        "重新取得真实设备实验授权",
    ):
        assert re.sub(r"\s+", "", required_remote_boundary) in compact_runbook

    assert "在官方控制面关闭七个模式，恢复原始全局档位" not in runbook
```

同时把现有 `required_boundary` 元组中的：

```python
        "执行真实面板会话",
```

替换为：

```python
        "执行真实远程会话",
```

- [ ] **Step 2: 运行测试并确认按预期失败**

Run:

```bash
uv run pytest tests/test_manifest.py::test_v2_discovery_docs_match_the_remote_operator_contract -v
```

Expected: FAIL because README and the runbook still require physical-panel operation and lack the remote safety prerequisites.

- [ ] **Step 3: 更新 README 的发现入口说明**

保持十个实验标签、只读边界和私有档案段落不变。将当前“每个 begin”段落替换为：

```markdown
每个 begin 先取得步骤基线，之后由用户按返回的固定提示在操作者控制面手工操作；控制面可以是
实体面板、奥普官方 App 或官方微信小程序。手机远程实验每次操作后先刷新控制面，再等待
15–30 秒并用 advance 取得下一阶段快照。模式、档位目标和温度实验各做两轮，并在每轮内人工
恢复原状态。官方控制面显示只用于核对人工操作，只有目标设备 Shadow `reported` 才是字段证据。

远程实验必须预先保证浴室无人、无宠物、设备无遮挡，暂停可能控制 Q360 的自动化，并安排家中
人员保持可联系。取消只清理本次软件会话，不会发恢复命令，也不会覆盖上一次成功报告；异常或
恢复不确定时必须停止会话，并由家中人员现场确认设备已安全停止。
```

- [ ] **Step 4: 更新运行手册的角色、授权门和固定操作措辞**

将开头角色说明改为：

```markdown
所有模式、全局档位和 AI 目标温度操作均由操作者通过控制面手工完成；控制面可以是实体面板、
奥普官方 App 或官方微信小程序。Home Assistant 不会替操作者开启、关闭、切换或恢复任何功能。
官方控制面显示只用于核对人工操作，不能作为字段证据；只有目标设备 Shadow `reported` 可以
确认字段语义。
```

把第六个授权门改为“完成远程预检后，执行真实远程会话”，把主标题“执行真实面板会话”改为
“执行真实远程会话”。固定实验表和正常阶段中的“实体面板”“目视确认”分别改为“操作者控制面”
和“刷新或重开控制面确认”，但保留现场人员异常接管时的“实体面板”。

- [ ] **Step 5: 写入精确的远程预检段落**

用下面内容替换“会话前检查”正文：

```markdown
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
- 准备在每次手机操作并刷新后等待 15–30 秒，并在 120 秒阶段期限内调用下一次 Action。

`all_modes_off_confirmed: true` 只表示操作者完成了上述关闭检查，不是字段证据。任何前提不成立
都不得调用 `aupu_q360.start_discovery`。
```

保留原来的 `start_discovery` 调用与 `discovery_ready_for_step` 成功条件。

- [ ] **Step 6: 让每个正常实验阶段都使用同一个手机节奏**

在静置、模式、全局档位和 AI 温度四节中明确使用以下固定节奏：

```markdown
每次 `begin_discovery_step` 后，只在官方控制面执行当前提示要求的一个操作；刷新或重开页面，
确认它显示刚请求的状态，等待 15–30 秒，再调用 `aupu_q360.advance_discovery_step`。不得同时
改变其他模式、档位或温度。只有 `discovery_cycle_recorded` 才允许开始下一轮。
```

各实验仍分别保留下面的精确参数规则：

```text
idle_environment: round 1 和 round 2，不操作设备
七个模式: 各 round 1 和 round 2，开启后恢复关闭
global_fan_level: ventilation 载体，原档位之外四个目标各两轮，每轮恢复原档位并关闭载体
ai_target_temperature: ai_thermostatic_warmth 载体，同一相邻温度两轮，每轮恢复原温度并关闭载体
```

- [ ] **Step 7: 用远程严格停止策略替换恢复与停止节**

用下面正文替换现有“恢复不确定与停止条件”：

```markdown
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
上述顺序远程恢复，否则直接通知现场人员接管。恢复 HA 后只做只读核查。
```

在“完成与本地核验”开头补充正常结束要求：通过官方控制面确认七个模式全关、原始全局档位和
原始 AI 温度均已恢复；正常完成不强制现场人员逐步骤检查。

- [ ] **Step 8: 运行聚焦测试并检查文档差异**

Run:

```bash
uv run pytest tests/test_manifest.py::test_v2_discovery_docs_match_the_remote_operator_contract -v
uv run ruff check tests/test_manifest.py
uv run ruff format --check tests/test_manifest.py
git diff --check
git diff -- README.md docs/q360-read-only-discovery-runbook.md tests/test_manifest.py
```

Expected: all commands PASS; diff preserves ten experiment labels, five Action names, fixed archive boundaries, reported-only evidence, and all existing secret exclusions.

- [ ] **Step 9: 检查并提交 Task 2**

Run:

```bash
git status --short
git diff --name-only
```

Expected: Task 1 is already committed; only `README.md`、运行手册、`tests/test_manifest.py` and the untracked plan are present.

After the task's commit authorization is in force:

```bash
git add README.md docs/q360-read-only-discovery-runbook.md tests/test_manifest.py
git commit -m "docs(状态发现): 改为手机远程实验流程"
```

---

### Task 3: 发布 `0.2.1` 并完成全量本地验证

**Files:**

- Modify: `tests/test_manifest.py:16`
- Modify: `pyproject.toml:3`
- Modify: `custom_components/aupu_q360/manifest.json:11`
- Modify: `custom_components/aupu_q360/const.py:4`
- Modify mechanically: `uv.lock:686-689`

**Interfaces:**

- Consumes: Task 1 和 Task 2 已通过的提示、翻译与文档契约。
- Produces: 所有项目版本源一致的 `0.2.1` 发布候选，不改变 Python floor、依赖解析或运行时接口。

- [ ] **Step 1: 先把版本契约改为 `0.2.1`**

在 `tests/test_manifest.py` 中只修改：

```python
VERSION = "0.2.1"
```

- [ ] **Step 2: 运行版本测试并确认按预期失败**

Run:

```bash
uv run pytest tests/test_manifest.py::test_manifest_is_hacs_installable -v
```

Expected: FAIL because manifest、`pyproject.toml`、`const.py` and the root package in `uv.lock` still contain `0.2.0`.

- [ ] **Step 3: 最小更新四个版本源和 lock file**

应用以下精确值：

```toml
# pyproject.toml
version = "0.2.1"
```

```json
{
  "version": "0.2.1"
}
```

上面的对象表示 manifest 中需替换的单个属性；保留文件中的其他属性及其顺序。

```python
# custom_components/aupu_q360/const.py
INTEGRATION_VERSION = "0.2.1"
```

然后只让 uv 机械更新本地 root package 版本：

```bash
uv lock --offline
```

检查 `uv.lock` 中 `name = "aupu-q360-ha"` 对应的版本为 `0.2.1`，其他名为 `0.2.0` 的第三方包
不得被替换。运行 `git diff -- uv.lock`，如果出现依赖版本、source、resolution marker 或哈希变化，
停止并调查，不能提交扩大后的 lock diff。

- [ ] **Step 4: 运行版本和完整 manifest 契约测试**

Run:

```bash
uv run pytest tests/test_manifest.py -v
uv lock --check --offline
```

Expected: all tests PASS and the lock file is current without network access.

- [ ] **Step 5: 运行完整本地验证矩阵**

Run each command separately and stop on the first failure:

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

Expected: every command exits `0`. Tests use only synthetic data and local HA runtime; no real login, archive read, discovery session, device operation, deployment or service restart occurs.

- [ ] **Step 6: 做发布范围和只读边界审计**

Run:

```bash
git diff --name-status HEAD
git diff -- custom_components/aupu_q360/services.yaml custom_components/aupu_q360/discovery.py custom_components/aupu_q360/discovery_catalog.py custom_components/aupu_q360/discovery_models.py custom_components/aupu_q360/discovery_store.py custom_components/aupu_q360/discovery_report_schema.py custom_components/aupu_q360/raw_discovery_archive.py
! rg -n "shadow/update|state\.desired|set_light|CONTROL_PATH|AupuApiClient|official AUPU app|微信小程序" custom_components/aupu_q360/discovery*.py custom_components/aupu_q360/raw_discovery_archive.py custom_components/aupu_q360/services.py
git status --short
```

Expected:

- the first command lists only Task 3 的四个版本文件 and `tests/test_manifest.py`；Task 1/2 已分别提交；
- the second command has no output；
- the third command has no output, proving the discovery modules contain no forbidden control reference or official-control-surface integration；
- the plan file may remain untracked and `.codegraph/` does not appear in staged content.

- [ ] **Step 7: 检查并提交 Task 3**

After the task's commit authorization is in force:

```bash
git add pyproject.toml uv.lock custom_components/aupu_q360/manifest.json custom_components/aupu_q360/const.py tests/test_manifest.py
git diff --cached --check
git diff --cached --name-status
git commit -m "chore(release): bump version to 0.2.1"
git status --short
```

Expected: the commit contains exactly four version-bearing files plus `tests/test_manifest.py`; afterward only the untracked plan remains.

## Post-Implementation Authorization Gates

所有三个本地提交和完整验证通过后停止，报告精确提交、验证结果和工作树状态。随后按顺序处理：

1. 用户审查提交并单独授权推送；不得 force push。
2. 推送后重新核对远端提交，再单独授权同步 `custom_components/aupu_q360` 到实际 HA `/config`。
3. 同步并通过 `check_config` 后，单独授权重启 Home Assistant；不修改 Compose、不重建容器、
   不升级 HA 镜像。
4. 重启后做无 discovery 烟测：版本 `0.2.1`、HTTP、Config Entry、灯光、connectivity、WSS、
   AUPU 错误日志和原始档案空闲状态。
5. 烟测通过后，单独授权启用原始档案 Options 和真实手机远程实验。真实实验必须按运行手册
   完成当次远程预检；任何异常进入现场兜底，不能读取或输出原始档案内容。
