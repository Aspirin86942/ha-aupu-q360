"""Temporary in-memory reported-only field probe for Q360 development."""

from __future__ import annotations

import asyncio
import re
import secrets
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Literal

from homeassistant.exceptions import HomeAssistantError

from .shadow import AcceptedShadow

type ProbeErrorCode = Literal[
    "probe_busy",
    "probe_inactive",
    "probe_wss_unavailable",
    "probe_snapshot_timeout",
    "probe_invalid_payload",
]
type ProbeValue = bool | int
type PublicProbeValue = ProbeValue | None
type PublicProbeChange = dict[str, str | PublicProbeValue]
type ProbeResponse = dict[str, str | int | list[PublicProbeChange]]
type PrepareTransport = Callable[[], Awaitable[None]]
type RequestShadowGet = Callable[[str], Awaitable[None]]
type ProbeObserver = Callable[[AcceptedShadow], None]
type ActivateObserver = Callable[[ProbeObserver, Callable[[], None]], None]
type DeactivateObserver = Callable[[], None]

_PATH_ID = re.compile(r"[0-9]{1,10}")
_MIN_INTEGER = -1000
_MAX_INTEGER = 1000
_MAX_PATHS = 256
_MAX_CHANGES = 128


class ProbeError(HomeAssistantError):
    """Expose one fixed probe error code without untrusted context."""

    def __init__(self, error_code: ProbeErrorCode) -> None:
        self.error_code = error_code
        super().__init__(error_code)

    def __repr__(self) -> str:
        return f"ProbeError(error_code={self.error_code!r})"


@dataclass(frozen=True, slots=True)
class ProbeChange:
    """One safe scalar change at a normalized synthetic path."""

    path: str
    before: PublicProbeValue
    after: PublicProbeValue

    def to_public(self) -> PublicProbeChange:
        return {"path": self.path, "before": self.before, "after": self.after}


def extract_probe_snapshot(state: object, device_id: str) -> dict[str, ProbeValue]:
    """Extract only bounded scalars from the target reported device root."""
    if not isinstance(state, dict) or not isinstance(device_id, str) or not device_id:
        raise ProbeError("probe_invalid_payload")
    reported = state.get("reported")
    if not isinstance(reported, dict):
        raise ProbeError("probe_invalid_payload")
    device_state = reported.get(device_id)
    if not isinstance(device_state, dict):
        raise ProbeError("probe_invalid_payload")

    snapshot: dict[str, ProbeValue] = {}
    for service_id, service in device_state.items():
        if not isinstance(service_id, str) or _PATH_ID.fullmatch(service_id) is None:
            raise ProbeError("probe_invalid_payload")
        if not isinstance(service, dict):
            raise ProbeError("probe_invalid_payload")
        if "properties" not in service:
            continue
        properties = service["properties"]
        if not isinstance(properties, dict):
            raise ProbeError("probe_invalid_payload")
        for property_id, value in properties.items():
            if not isinstance(property_id, str) or _PATH_ID.fullmatch(property_id) is None:
                raise ProbeError("probe_invalid_payload")
            retained = _retained_value(value)
            if retained is None:
                continue
            snapshot[f"service/{service_id}/property/{property_id}"] = retained
            if len(snapshot) > _MAX_PATHS:
                raise ProbeError("probe_invalid_payload")
    return snapshot


def diff_probe_snapshots(
    before: Mapping[str, ProbeValue],
    after: Mapping[str, ProbeValue],
) -> tuple[ProbeChange, ...]:
    """Return a bounded, deterministic union diff without bool/int coercion."""
    changes: list[ProbeChange] = []
    for path in sorted(before.keys() | after.keys()):
        before_present = path in before
        after_present = path in after
        before_value = before.get(path)
        after_value = after.get(path)
        if (
            before_present
            and after_present
            and type(before_value) is type(after_value)
            and before_value == after_value
        ):
            continue
        changes.append(
            ProbeChange(
                path=path,
                before=before_value if before_present else None,
                after=after_value if after_present else None,
            )
        )
        if len(changes) > _MAX_CHANGES:
            raise ProbeError("probe_invalid_payload")
    return tuple(changes)


def _response(
    *,
    state: Literal["active", "inactive"],
    message_code: Literal["probe_started", "probe_sampled", "probe_stopped"],
    sample_count: int,
    changes: tuple[ProbeChange, ...] = (),
) -> ProbeResponse:
    return {
        "state": state,
        "message_code": message_code,
        "sample_count": sample_count,
        "changes": [change.to_public() for change in changes],
    }


class PanelStateProbe:
    """Own one ephemeral sequence of correlated reported snapshots."""

    def __init__(
        self,
        *,
        device_id: str,
        prepare_transport: PrepareTransport,
        request_shadow_get: RequestShadowGet,
        activate_observer: ActivateObserver,
        deactivate_observer: DeactivateObserver,
        probe_available: Callable[[], bool],
        snapshot_timeout: float = 10.0,
    ) -> None:
        if not device_id or snapshot_timeout <= 0:
            raise ValueError("invalid probe configuration")
        self._device_id = device_id
        self._prepare_transport = prepare_transport
        self._request_shadow_get = request_shadow_get
        self._activate_observer = activate_observer
        self._deactivate_observer = deactivate_observer
        self._probe_available = probe_available
        self._snapshot_timeout = snapshot_timeout
        self._operation_lock = asyncio.Lock()
        self._active = False
        self._observer_active = False
        self._snapshot: dict[str, ProbeValue] | None = None
        self._sample_count = 0
        self._pending_token: str | None = None
        self._pending_future: asyncio.Future[dict[str, ProbeValue]] | None = None

    @property
    def active(self) -> bool:
        return self._active

    @property
    def pending_token(self) -> str | None:
        return self._pending_token

    async def async_start(self) -> ProbeResponse:
        if self._operation_lock.locked() or self._active:
            raise ProbeError("probe_busy")
        async with self._operation_lock:
            try:
                await self._prepare_transport()
                if not self._probe_available():
                    raise ProbeError("probe_wss_unavailable")
                self._active = True
                self._activate_observer(self.observe_shadow, self.cancel_from_transport)
                self._observer_active = True
                self._snapshot = await self._async_snapshot()
                return _response(
                    state="active",
                    message_code="probe_started",
                    sample_count=0,
                )
            except asyncio.CancelledError:
                self._clear()
                raise
            except ProbeError:
                self._clear()
                raise
            except Exception:  # noqa: BLE001 - external callbacks map to one fixed code
                self._clear()
                raise ProbeError("probe_wss_unavailable") from None

    async def async_sample(self) -> ProbeResponse:
        if self._operation_lock.locked():
            raise ProbeError("probe_busy")
        async with self._operation_lock:
            if not self._active or self._snapshot is None:
                raise ProbeError("probe_inactive")
            try:
                current = await self._async_snapshot()
                changes = diff_probe_snapshots(self._snapshot, current)
                self._snapshot = current
                self._sample_count += 1
                return _response(
                    state="active",
                    message_code="probe_sampled",
                    sample_count=self._sample_count,
                    changes=changes,
                )
            except asyncio.CancelledError:
                self._clear()
                raise
            except ProbeError:
                self._clear()
                raise
            except Exception:  # noqa: BLE001 - external callbacks map to one fixed code
                self._clear()
                raise ProbeError("probe_wss_unavailable") from None

    async def async_stop_probe(self) -> ProbeResponse:
        async with self._operation_lock:
            sample_count = self._sample_count
            self._clear()
            return _response(
                state="inactive",
                message_code="probe_stopped",
                sample_count=sample_count,
            )

    async def async_stop(self) -> None:
        async with self._operation_lock:
            self._clear()

    def observe_shadow(self, message: AcceptedShadow) -> None:
        future = self._pending_future
        if (
            future is None
            or future.done()
            or message.topic_kind != "get"
            or message.client_token != self._pending_token
        ):
            return
        try:
            snapshot = extract_probe_snapshot(message.state, self._device_id)
        except ProbeError as err:
            future.set_exception(err)
        else:
            future.set_result(snapshot)

    def cancel_from_transport(self) -> None:
        self._clear(error=ProbeError("probe_wss_unavailable"))

    async def _async_snapshot(self) -> dict[str, ProbeValue]:
        if not self._active or not self._probe_available():
            raise ProbeError("probe_wss_unavailable")
        token = f"disc-{secrets.token_hex(16)}"
        future: asyncio.Future[dict[str, ProbeValue]] = asyncio.get_running_loop().create_future()
        self._pending_token = token
        self._pending_future = future
        try:
            await self._request_shadow_get(token)
            async with asyncio.timeout(self._snapshot_timeout):
                return await future
        except TimeoutError:
            raise ProbeError("probe_snapshot_timeout") from None
        finally:
            if self._pending_future is future:
                self._pending_token = None
                self._pending_future = None

    def _clear(self, *, error: ProbeError | None = None) -> None:
        future = self._pending_future
        self._pending_token = None
        self._pending_future = None
        if future is not None and not future.done():
            if error is None:
                future.cancel()
            else:
                future.set_exception(error)
        self._active = False
        self._snapshot = None
        self._sample_count = 0
        if self._observer_active:
            self._observer_active = False
            self._deactivate_observer()


def _retained_value(value: object) -> ProbeValue | None:
    if type(value) is bool:
        return value
    if type(value) is int and _MIN_INTEGER <= value <= _MAX_INTEGER:
        return value
    return None
