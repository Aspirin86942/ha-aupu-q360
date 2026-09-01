"""Tests for repository metadata consumed by Home Assistant and HACS."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path

import yaml

DOMAIN = "aupu_q360"
NAME = "AUPU Q360"
VERSION = "0.1.0"


def _load_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _key_tree(value: object) -> object:
    """Return only mapping keys so translated prose may differ freely."""
    if isinstance(value, dict):
        return {key: _key_tree(nested) for key, nested in value.items()}
    return None


def _load_yaml(path: Path) -> dict[str, object]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _scalar_strings(value: object) -> list[str]:
    if isinstance(value, Mapping):
        return [text for nested in value.values() for text in _scalar_strings(nested)]
    if isinstance(value, list):
        return [text for nested in value for text in _scalar_strings(nested)]
    return [value] if isinstance(value, str) else []


def test_manifest_is_hacs_installable(project_root: Path) -> None:
    """Expose accidental metadata regressions before HACS installs the integration."""
    manifest = _load_json(project_root / "custom_components/aupu_q360/manifest.json")
    hacs = _load_json(project_root / "hacs.json")
    pyproject = (project_root / "pyproject.toml").read_text(encoding="utf-8")

    assert manifest["domain"] == DOMAIN
    assert manifest["name"] == NAME
    assert manifest["version"] == VERSION
    assert hacs["name"] == manifest["name"]
    assert 'name = "aupu-q360-ha"' in pyproject
    assert f'version = "{manifest["version"]}"' in pyproject
    assert manifest["config_flow"] is True
    assert manifest["iot_class"] == "cloud_push"
    assert manifest["integration_type"] == "device"
    assert manifest["requirements"] == []
    assert "documentation" not in manifest
    assert "issue_tracker" not in manifest


def test_english_and_simplified_chinese_flow_keys_match(project_root: Path) -> None:
    """Catch a flow branch becoming unavailable in one language."""
    strings = _load_json(project_root / "custom_components/aupu_q360/strings.json")
    translation = _load_json(project_root / "custom_components/aupu_q360/translations/zh-Hans.json")

    assert _key_tree(translation) == _key_tree(strings)
    assert set(strings) == {"config", "options", "issues"}
    config = strings["config"]
    options = strings["options"]
    assert isinstance(config, dict)
    assert isinstance(options, dict)
    assert set(config["step"]) == {
        "user",
        "confirm_wss",
        "reauth_method",
        "reauth_sms_send",
        "reauth_sms_code",
        "reauth_manual_token",
    }
    assert set(config["error"]) == {
        "invalid_signer",
        "invalid_token",
        "expired_token",
        "invalid_device",
        "cannot_connect",
        "invalid_phone",
        "invalid_sms_code",
        "cannot_send_sms",
        "sms_rate_limited",
        "sms_code_expired",
    }
    assert set(config["abort"]) == {
        "already_configured",
        "invalid_state",
        "invalid_entry",
        "reauth_successful",
    }
    assert set(options["step"]) == {"init", "confirm_wss"}
    assert set(options["error"]) == {
        "invalid_token",
        "expired_token",
        "cannot_connect",
    }
    assert set(options["abort"]) == {"invalid_state", "invalid_entry"}


def test_readme_documents_safe_offline_install_and_operations(project_root: Path) -> None:
    """Keep the operator path complete without embedding credential-shaped examples."""
    readme = (project_root / "README.md").read_text(encoding="utf-8")

    for heading in (
        "## 支持范围",
        "## HACS 安装",
        "## 添加集成",
        "## 凭据、JWT 与重新认证",
        "## 状态与控制语义",
        "## 安全与备份",
        "## 升级与卸载",
        "## 故障排查",
        "## 发布前说明",
    ):
        assert heading in readme
    for required_text in (
        "你的真实 GitHub 仓库 URL",
        "重启 Home Assistant",
        "私有签名 JSON",
        "60 秒",
        "5 分钟",
        "手工 Token",
        "Repair",
        "推定状态",
        "诊断白名单",
        "固定错误码",
        "加密",
        "取暖",
        "换气",
        "烘干",
        "摆风",
    ):
        assert required_text in readme

    assert "不会自动刷新或自动续期" in readme
    assert not re.search(r"(?<!不)会自动刷新|将自动刷新|可自动刷新|支持自动刷新", readme)
    assert not re.search(r"\b1[3-9]\d{9}\b", readme)
    assert not re.search(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b", readme)
    assert not re.search(r"https://github\.com/[^/\s]+/[^/\s]+", readme)

    release_steps = (
        "发布到真实 GitHub 仓库",
        "补充真实 `documentation` 与 `issue_tracker`",
        "运行 HACS 和 hassfest 验证",
        "在 HACS 中添加真实仓库 URL",
        "重启 Home Assistant",
        "添加集成",
    )
    positions = [readme.index(step) for step in release_steps]
    assert positions == sorted(positions)


def test_ci_has_exactly_four_minimal_non_publishing_jobs(project_root: Path) -> None:
    """Catch CI losing a validation gate or gaining an artifact publication path."""
    workflow = _load_yaml(project_root / ".github/workflows/validate.yml")
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    assert set(jobs) == {"offline-quality", "ha-runtime", "hacs", "hassfest"}
    assert workflow["permissions"] == {"contents": "read"}

    assert jobs["offline-quality"] == {
        "runs-on": "ubuntu-latest",
        "steps": [
            {"uses": "actions/checkout@v4"},
            {
                "uses": "actions/setup-python@v5",
                "with": {"python-version": "3.13"},
            },
            {"uses": "astral-sh/setup-uv@v6"},
            {"run": "uv sync --locked"},
            {"run": "uv run pytest"},
            {"run": "uv run ruff check ."},
            {"run": "uv run ruff format --check ."},
            {"run": "uv run mypy custom_components/aupu_q360"},
            {"run": "uv run python scripts/check_no_secrets.py"},
        ],
    }
    assert jobs["ha-runtime"] == {
        "runs-on": "ubuntu-latest",
        "env": {"AUPU_RUN_HA_RUNTIME": "1"},
        "steps": [
            {"uses": "actions/checkout@v4"},
            {
                "uses": "actions/setup-python@v5",
                "with": {"python-version": "3.13"},
            },
            {"uses": "astral-sh/setup-uv@v6"},
            {"run": "uv sync --locked --group ha-test"},
            {"run": "uv run --group ha-test pytest tests/ha_runtime -m ha_runtime -v"},
        ],
    }
    assert jobs["hacs"] == {
        "runs-on": "ubuntu-latest",
        "steps": [
            {"uses": "actions/checkout@v4"},
            {"uses": "hacs/action@main", "with": {"category": "integration"}},
        ],
    }
    assert jobs["hassfest"] == {
        "runs-on": "ubuntu-latest",
        "steps": [
            {"uses": "actions/checkout@v4"},
            {"uses": "home-assistant/actions/hassfest@master"},
        ],
    }

    strings = _scalar_strings(workflow)
    assert not any("upload-artifact" in value for value in strings)
    assert not any(".private" in value for value in strings)
    assert not any("local-evidence" in value for value in strings)
