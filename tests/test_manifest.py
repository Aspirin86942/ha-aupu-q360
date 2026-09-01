"""Tests for repository metadata consumed by Home Assistant and HACS."""

from __future__ import annotations

import json
from pathlib import Path


def test_manifest_is_hacs_installable(project_root: Path) -> None:
    """Expose accidental metadata regressions before HACS installs the integration."""
    manifest = json.loads(
        (project_root / "custom_components/aupu_q360/manifest.json").read_text()
    )
    assert manifest["domain"] == "aupu_q360"
    assert manifest["config_flow"] is True
    assert manifest["iot_class"] == "cloud_push"
    assert manifest["requirements"] == []
    assert json.loads((project_root / "hacs.json").read_text())["name"] == "AUPU Q360"
