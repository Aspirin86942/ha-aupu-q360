"""Strictly allowlisted diagnostics for AUPU Q360 config entries."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .auth import BearerCredential
from .const import INTEGRATION_VERSION
from .discovery_models import JsonObject
from .models import AupuRuntimeData

_ONE_DAY = timedelta(days=1)
_SEVEN_DAYS = timedelta(days=7)
_ERROR_CODES = frozenset(
    {
        "none",
        "aupu_error",
        "authentication_failed",
        "rate_limited",
        "temporary_failure",
        "protocol_error",
        "runtime_stopped",
    }
)
_STATE_SOURCES = frozenset({"unknown", "command", "reported", "desired", "get_reported"})

ExpiryBucket = Literal["expired", "<24h", "<7d", ">=7d", "unknown"]


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry[AupuRuntimeData],
) -> JsonObject:
    """Return only non-secret scalar health signals from a loaded entry."""
    del hass
    runtime = _safe_getattr(entry, "runtime_data", None)
    credential = _safe_getattr(runtime, "credential", None)
    coordinator = _safe_getattr(runtime, "coordinator", None)
    runtime_complete = isinstance(credential, BearerCredential) and coordinator is not None

    result: JsonObject = {}
    result["integration_version"] = INTEGRATION_VERSION
    result["authentication_expiry_bucket"] = (
        _safe_expiry_bucket(credential) if runtime_complete else "unknown"
    )
    result["wss_enabled"] = (
        _safe_bool(_safe_getattr(runtime, "use_wss", False)) if runtime_complete else False
    )
    result["wss_connected"] = (
        _safe_bool(_safe_getattr(coordinator, "wss_connected", False))
        if runtime_complete
        else False
    )
    result["wss_healthy"] = (
        _safe_bool(_safe_getattr(coordinator, "wss_healthy", False)) if runtime_complete else False
    )
    result["last_error_code"] = (
        _safe_enum(
            _safe_getattr(coordinator, "last_error_code", "none"),
            _ERROR_CODES,
            "none",
        )
        if runtime_complete
        else "none"
    )
    result["light_state_source"] = (
        _safe_enum(
            _safe_getattr(coordinator, "light_state_source", "unknown"),
            _STATE_SOURCES,
            "unknown",
        )
        if runtime_complete
        else "unknown"
    )
    result["assumed_state"] = (
        _safe_bool(_safe_getattr(coordinator, "assumed_state", False))
        if runtime_complete
        else False
    )
    result["state_discovery"] = await _safe_discovery_report(runtime)
    return result


async def _safe_discovery_report(runtime: object) -> JsonObject:
    store = _safe_getattr(runtime, "discovery_store", None)
    loader = _safe_getattr(store, "async_load", None)
    if not callable(loader):
        return {"report_available": False}
    try:
        report = await cast(Any, loader)()
    except Exception:  # noqa: BLE001 - diagnostics must not expose Store details
        return {"report_available": False}
    if not isinstance(report, dict):
        return {"report_available": False}
    return {"report_available": True, "report": cast(JsonObject, report)}


def _expiry_bucket(credential: object, now: datetime) -> ExpiryBucket:
    """Coarsen the local expiry hint without returning either input instant."""
    if not isinstance(credential, BearerCredential) or credential.expires_at is None:
        return "unknown"
    remaining = credential.expires_at - now
    if remaining <= timedelta(0):
        return "expired"
    if remaining < _ONE_DAY:
        return "<24h"
    if remaining < _SEVEN_DAYS:
        return "<7d"
    return ">=7d"


def _safe_expiry_bucket(credential: object) -> ExpiryBucket:
    try:
        return _expiry_bucket(credential, _utcnow())
    except Exception:  # noqa: BLE001 - collapse secret-bearing runtime failures
        return "unknown"


def _safe_getattr(value: object, name: str, default: object) -> object:
    try:
        return getattr(value, name, default)
    except Exception:  # noqa: BLE001 - never propagate property text into HA logs
        return default


def _safe_bool(value: object) -> bool:
    return value if type(value) is bool else False


def _safe_enum(value: object, allowed: frozenset[str], default: str) -> str:
    return value if isinstance(value, str) and value in allowed else default


def _utcnow() -> datetime:
    """Return an injectable UTC clock for coarse expiry classification."""
    return datetime.now(UTC)
