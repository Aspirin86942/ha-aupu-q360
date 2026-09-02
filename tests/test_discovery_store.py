"""Tests for private atomic persistence of the latest discovery report."""

from __future__ import annotations

import importlib
import logging
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any, ClassVar

import pytest

from custom_components.aupu_q360.discovery_analysis import build_discovery_report
from custom_components.aupu_q360.discovery_sanitizer import validate_discovery_report


def _module():  # type: ignore[no-untyped-def]
    return importlib.import_module("custom_components.aupu_q360.discovery_store")


def _report(version: str = "0.1.1") -> dict[str, Any]:
    return build_discovery_report(
        integration_version=version,
        started_at=datetime(2026, 9, 2, 13, 47, tzinfo=UTC),
        wss_baseline_succeeded=True,
        steps=(),
    )


class FakeStore:
    """Model atomic replacement and record HA Store constructor options."""

    values: ClassVar[dict[str, object]] = {}
    instances: ClassVar[list[FakeStore]] = []

    def __init__(
        self,
        hass: object,
        version: int,
        key: str,
        private: bool = False,
        *,
        atomic_writes: bool = False,
    ) -> None:
        self.hass = hass
        self.version = version
        self.key = key
        self.private = private
        self.atomic_writes = atomic_writes
        self.fail_save = False
        self.fail_load = False
        self.fail_remove = False
        self.instances.append(self)

    async def async_load(self) -> object | None:
        if self.fail_load:
            raise RuntimeError("private-load-detail")
        return deepcopy(self.values.get(self.key))

    async def async_save(self, data: object) -> None:
        if self.fail_save:
            raise RuntimeError("private-save-detail")
        self.values[self.key] = deepcopy(data)

    async def async_remove(self) -> None:
        if self.fail_remove:
            raise RuntimeError("private-remove-detail")
        self.values.pop(self.key, None)


@pytest.fixture(autouse=True)
def _fake_store(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeStore.values.clear()
    FakeStore.instances.clear()
    monkeypatch.setattr(_module(), "Store", FakeStore)


def _validator(report: object) -> None:
    validate_discovery_report(
        report,
        forbidden_values=("123456789012345", "synthetic-entry-id"),
    )


@pytest.mark.asyncio
async def test_store_is_private_atomic_and_replaces_only_after_success() -> None:
    """Catch reports using a public file or a failed save destroying the prior one."""
    module = _module()
    store = module.DiscoveryReportStore(
        object(),
        "synthetic-entry-id",
        _validator,
    )
    backend = FakeStore.instances[-1]
    first = _report("0.1.1")
    second = _report("0.1.2")

    assert backend.version == 1
    assert backend.key == "aupu_q360.discovery.synthetic-entry-id"
    assert backend.private is True
    assert backend.atomic_writes is True

    await store.async_save(first)
    assert await store.async_load() == first

    backend.fail_save = True
    with pytest.raises(module.DiscoveryReportStoreError) as raised:
        await store.async_save(second)

    assert str(raised.value) == "discovery_report_storage_failed"
    backend.fail_save = False
    assert await store.async_load() == first

    await store.async_save(second)
    assert await store.async_load() == second


@pytest.mark.asyncio
async def test_load_rejects_corrupt_schema_and_store_errors_use_fixed_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Catch corrupted or exception-bearing storage content entering diagnostics."""
    module = _module()
    store = module.DiscoveryReportStore(object(), "synthetic-entry-id", _validator)
    backend = FakeStore.instances[-1]
    FakeStore.values[backend.key] = {"schema_version": 99, "private": "raw-value"}

    with caplog.at_level(logging.ERROR, logger=module.__name__):
        assert await store.async_load() is None
        backend.fail_load = True
        assert await store.async_load() is None

    assert "AUPU discovery report storage failed" in caplog.text
    assert "private-load-detail" not in caplog.text
    assert "raw-value" not in caplog.text


@pytest.mark.asyncio
async def test_remove_and_entry_removal_helper_clear_only_the_target_key() -> None:
    """Catch Config Entry removal deleting another entry's latest report."""
    module = _module()
    first = module.DiscoveryReportStore(object(), "entry-one", _validator)
    second = module.DiscoveryReportStore(object(), "entry-two", _validator)
    await first.async_save(_report())
    await second.async_save(_report())

    await first.async_remove()
    assert await first.async_load() is None
    assert await second.async_load() == _report()

    await module.DiscoveryReportStore.async_remove_for_entry(object(), "entry-two")
    assert FakeStore.values == {}
