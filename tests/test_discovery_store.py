"""Tests for private schema-v2 discovery report persistence."""

from __future__ import annotations

import importlib
import logging
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any, ClassVar

import pytest

from custom_components.aupu_q360.discovery_analysis import build_discovery_report
from custom_components.aupu_q360.discovery_report_schema import validate_discovery_report


def _module():  # type: ignore[no-untyped-def]
    return importlib.import_module("custom_components.aupu_q360.discovery_store")


def _report(version: str = "0.2.0") -> dict[str, Any]:
    return build_discovery_report(
        integration_version=version,
        started_at=datetime(2026, 9, 3, 0, 47, tzinfo=UTC),
        wss_baseline_succeeded=True,
        cycles=(),
    )


class FakeStore:
    """Model atomic replacement and record exact HA Store construction."""

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
        **extra: object,
    ) -> None:
        self.hass = hass
        self.version = version
        self.key = key
        self.private = private
        self.atomic_writes = atomic_writes
        self.extra = extra
        self.fail_save = False
        self.fail_load = False
        self.fail_remove = False
        self.load_calls = 0
        self.remove_calls = 0
        self.instances.append(self)

    async def async_load(self) -> object | None:
        self.load_calls += 1
        if self.fail_load:
            raise RuntimeError("private-load-detail")
        return deepcopy(self.values.get(self.key))

    async def async_save(self, data: object) -> None:
        if self.fail_save:
            raise RuntimeError("private-save-detail")
        self.values[self.key] = deepcopy(data)

    async def async_remove(self) -> None:
        self.remove_calls += 1
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
async def test_v2_store_is_private_atomic_and_preserves_prior_report_on_failure() -> None:
    """Catch the new schema using the legacy key or failed writes erasing prior data."""
    module = _module()
    store = module.DiscoveryReportStore(object(), "synthetic-entry-id", _validator)
    backend = FakeStore.instances[-1]
    first = _report("0.2.0")
    second = _report("0.2.1")

    assert backend.version == 1
    assert backend.key == "aupu_q360.discovery_v2.synthetic-entry-id"
    assert backend.private is True
    assert backend.atomic_writes is True
    assert backend.extra == {}

    await store.async_save(first)
    assert await store.async_load() == first

    backend.fail_save = True
    with pytest.raises(module.DiscoveryReportStoreError) as raised:
        await store.async_save(second)

    assert str(raised.value) == "discovery_report_storage_failed"
    backend.fail_save = False
    assert await store.async_load() == first


@pytest.mark.asyncio
async def test_normal_load_never_reads_or_migrates_the_legacy_v1_key() -> None:
    """Catch a v1 report being relabeled, rewritten, or deleted during v2 setup."""
    module = _module()
    legacy_key = "aupu_q360.discovery.synthetic-entry-id"
    FakeStore.values[legacy_key] = {
        "schema_version": 1,
        "private_legacy_marker": "must-stay-untouched",
    }

    store = module.DiscoveryReportStore(object(), "synthetic-entry-id", _validator)
    backend = FakeStore.instances[-1]

    assert await store.async_load() is None
    assert backend.key == "aupu_q360.discovery_v2.synthetic-entry-id"
    assert backend.load_calls == 1
    assert FakeStore.values == {
        legacy_key: {
            "schema_version": 1,
            "private_legacy_marker": "must-stay-untouched",
        }
    }
    assert backend.extra == {}


@pytest.mark.asyncio
async def test_load_rejects_corrupt_v2_schema_with_fixed_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Catch corrupted or exception-bearing Store content entering diagnostics."""
    module = _module()
    store = module.DiscoveryReportStore(object(), "synthetic-entry-id", _validator)
    backend = FakeStore.instances[-1]
    FakeStore.values[backend.key] = {"schema_version": 2, "private": "raw-value"}

    with caplog.at_level(logging.ERROR, logger=module.__name__):
        assert await store.async_load() is None
        backend.fail_load = True
        assert await store.async_load() is None

    assert "AUPU discovery report storage failed" in caplog.text
    assert "private-load-detail" not in caplog.text
    assert "raw-value" not in caplog.text


@pytest.mark.asyncio
async def test_entry_removal_deletes_exact_v2_and_legacy_keys_only() -> None:
    """Catch Config Entry deletion inspecting archives or deleting another entry's reports."""
    module = _module()
    for entry_id in ("entry-one", "entry-two"):
        FakeStore.values[f"aupu_q360.discovery_v2.{entry_id}"] = _report()
        FakeStore.values[f"aupu_q360.discovery.{entry_id}"] = {
            "schema_version": 1,
        }

    await module.DiscoveryReportStore.async_remove_for_entry(object(), "entry-one")

    assert set(FakeStore.values) == {
        "aupu_q360.discovery_v2.entry-two",
        "aupu_q360.discovery.entry-two",
    }
    removed = FakeStore.instances[-2:]
    assert {store.key for store in removed} == {
        "aupu_q360.discovery_v2.entry-one",
        "aupu_q360.discovery.entry-one",
    }
    assert all(store.remove_calls == 1 for store in removed)
    assert all(store.load_calls == 0 for store in removed)


@pytest.mark.asyncio
async def test_removal_attempts_both_keys_and_uses_only_fixed_failure_text(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Catch one failed removal skipping the other key or leaking backend details."""
    module = _module()
    original_remove = FakeStore.async_remove

    async def fail_v2_only(store: FakeStore) -> None:
        if ".discovery_v2." in store.key:
            store.remove_calls += 1
            raise RuntimeError("private-remove-detail")
        await original_remove(store)

    monkeypatch.setattr(FakeStore, "async_remove", fail_v2_only)
    FakeStore.values["aupu_q360.discovery_v2.entry-one"] = _report()
    FakeStore.values["aupu_q360.discovery.entry-one"] = {"schema_version": 1}

    with (
        caplog.at_level(logging.ERROR, logger=module.__name__),
        pytest.raises(module.DiscoveryReportStoreError),
    ):
        await module.DiscoveryReportStore.async_remove_for_entry(object(), "entry-one")

    assert "aupu_q360.discovery_v2.entry-one" in FakeStore.values
    assert "aupu_q360.discovery.entry-one" not in FakeStore.values
    assert "AUPU discovery report storage failed" in caplog.text
    assert "private-remove-detail" not in caplog.text
