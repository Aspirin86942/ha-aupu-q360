"""Bounded, one-at-a-time Q360 read-only state discovery sessions."""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from .discovery_analysis import build_discovery_report, diff_snapshots
from .discovery_models import (
    DiscoveryState,
    JsonObject,
    SanitizedValue,
    StepEvidence,
    StepLabel,
)
from .discovery_sanitizer import (
    DiscoverySanitizationError,
    DiscoverySanitizer,
)
from .errors import (
    DiscoveryBusyError,
    DiscoveryError,
    DiscoveryInvalidTransitionError,
    DiscoveryReportSaveFailedError,
    DiscoveryResourceLimitError,
    DiscoverySessionExpiredError,
    DiscoverySnapshotTimeoutError,
    DiscoveryStepExpiredError,
    DiscoveryWssUnavailableError,
)
from .shadow import AcceptedShadow

Snapshot = dict[str, SanitizedValue]
SnapshotRequester = Callable[[str], Awaitable[None]]
ReportSaver = Callable[[JsonObject], Awaitable[None]]
SanitizerFactory = Callable[[bytes], DiscoverySanitizer]
ReportValidator = Callable[[object], object]
Observer = Callable[[AcceptedShadow], None]
CancelCallback = Callable[[], None]
ObserverActivator = Callable[[Observer, CancelCallback], None]
Clock = Callable[[], datetime]

_DEFAULT_SNAPSHOT_TIMEOUT = 10.0
_DEFAULT_STEP_TIMEOUT = 120.0
_DEFAULT_SESSION_TIMEOUT = 1200.0
_DEFAULT_MAX_CHANGES = 256


def _utc_now() -> datetime:
    return datetime.now(UTC)


class StateDiscoverySession:
    """Own all short-lived discovery state for one loaded Config Entry."""

    def __init__(
        self,
        *,
        request_shadow_get: SnapshotRequester,
        save_report: ReportSaver,
        sanitizer_factory: SanitizerFactory,
        validate_report: ReportValidator,
        activate_observer: ObserverActivator,
        deactivate_observer: Callable[[], None],
        discovery_available: Callable[[], bool],
        integration_version: str,
        now: Clock = _utc_now,
        snapshot_timeout_seconds: float = _DEFAULT_SNAPSHOT_TIMEOUT,
        step_timeout_seconds: float = _DEFAULT_STEP_TIMEOUT,
        session_timeout_seconds: float = _DEFAULT_SESSION_TIMEOUT,
        max_changes_per_step: int = _DEFAULT_MAX_CHANGES,
    ) -> None:
        self._request_shadow_get = request_shadow_get
        self._save_report = save_report
        self._sanitizer_factory = sanitizer_factory
        self._validate_report = validate_report
        self._activate_observer = activate_observer
        self._deactivate_observer = deactivate_observer
        self._discovery_available = discovery_available
        self._integration_version = integration_version
        self._now = now
        self._snapshot_timeout_seconds = snapshot_timeout_seconds
        self._step_timeout_seconds = step_timeout_seconds
        self._session_timeout_seconds = session_timeout_seconds
        self._max_changes_per_step = max_changes_per_step

        self._state = DiscoveryState.IDLE
        self._last_error_code = "none"
        self._observer_active = False
        self._session_key: bytes | None = None
        self._sanitizer: DiscoverySanitizer | None = None
        self._started_at: datetime | None = None
        self._baseline: Snapshot | None = None
        self._pending_token: str | None = None
        self._pending_future: asyncio.Future[Snapshot] | None = None
        self._step_label: StepLabel | None = None
        self._step_before: Snapshot | None = None
        self._step_last_values: Snapshot | None = None
        self._step_transient: list[tuple[str, SanitizedValue]] = []
        self._step_invalid = False
        self._steps: list[StepEvidence] = []
        self._session_timer: asyncio.Task[None] | None = None
        self._step_timer: asyncio.Task[None] | None = None
        self._owned_tasks: set[asyncio.Task[None]] = set()

    @property
    def state(self) -> DiscoveryState:
        """Return the current controlled state without exposing session content."""
        return self._state

    @property
    def last_error_code(self) -> str:
        """Return only the latest fixed discovery error code."""
        return self._last_error_code

    async def async_start(self) -> None:
        """Attach observation and establish the session's all-off baseline."""
        if self._state is not DiscoveryState.IDLE:
            raise DiscoveryBusyError
        if not self._discovery_available():
            self._last_error_code = DiscoveryWssUnavailableError.error_code
            raise DiscoveryWssUnavailableError

        self._last_error_code = "none"
        self._state = DiscoveryState.BASELINING
        self._session_key = secrets.token_bytes(32)
        self._sanitizer = self._sanitizer_factory(self._session_key)
        started_at = self._now()
        if started_at.tzinfo is None or started_at.utcoffset() is None:
            self._cleanup()
            raise ValueError("Discovery clock must return an aware datetime")
        self._started_at = started_at.astimezone(UTC)
        try:
            self._activate_observer(
                self.async_observe_shadow,
                lambda: self.cancel_from_transport(DiscoveryWssUnavailableError.error_code),
            )
            self._observer_active = True
            self._session_timer = self._create_expiry_task(
                self._session_timeout_seconds,
                DiscoverySessionExpiredError.error_code,
                "aupu_q360_discovery_session_timeout",
            )
            self._baseline = await self._async_request_snapshot()
        except asyncio.CancelledError:
            self._cleanup()
            raise
        except DiscoveryError as err:
            if self._state is not DiscoveryState.IDLE:
                self._cleanup(err.error_code)
            raise
        except Exception:  # noqa: BLE001 - hide injected observer failures
            self._cleanup(DiscoveryWssUnavailableError.error_code)
            raise DiscoveryWssUnavailableError from None
        self._state = DiscoveryState.READY

    async def async_begin_step(
        self,
        capability: str,
        target: str,
        round_number: int,
    ) -> None:
        """Take the pre-action snapshot and enter bounded update observation."""
        self._require_state(DiscoveryState.READY)
        try:
            label = StepLabel(capability, target, round_number)
        except ValueError:
            raise DiscoveryInvalidTransitionError from None

        self._state = DiscoveryState.STEP_BASELINING
        self._step_label = label
        self._step_timer = self._create_expiry_task(
            self._step_timeout_seconds,
            DiscoveryStepExpiredError.error_code,
            "aupu_q360_discovery_step_timeout",
        )
        try:
            before = await self._async_request_snapshot()
        except asyncio.CancelledError:
            self._cleanup()
            raise
        except DiscoveryError as err:
            if self._state is not DiscoveryState.IDLE:
                self._cleanup(err.error_code)
            raise
        self._step_before = before
        self._step_last_values = dict(before)
        self._step_transient = []
        self._step_invalid = False
        self._state = DiscoveryState.OBSERVING

    async def async_complete_step(self) -> StepEvidence:
        """Take the final snapshot, retain sanitized evidence, and return ready."""
        self._require_state(DiscoveryState.OBSERVING)
        label = self._step_label
        if label is None:
            self._cleanup(DiscoveryInvalidTransitionError.error_code)
            raise DiscoveryInvalidTransitionError
        if self._step_invalid:
            evidence = StepEvidence(
                label=label,
                snapshot_succeeded=False,
                baseline_restored=None,
                changes=(),
                invalid=True,
            )
            self._steps.append(evidence)
            self._clear_step()
            self._state = DiscoveryState.READY
            self._last_error_code = DiscoveryResourceLimitError.error_code
            raise DiscoveryResourceLimitError

        self._state = DiscoveryState.STEP_FINALIZING
        try:
            after = await self._async_request_snapshot()
        except asyncio.CancelledError:
            self._cleanup()
            raise
        except DiscoveryError as err:
            if self._state is not DiscoveryState.IDLE:
                self._cleanup(err.error_code)
            raise

        before = self._step_before
        baseline = self._baseline
        if before is None or baseline is None:
            self._cleanup(DiscoveryInvalidTransitionError.error_code)
            raise DiscoveryInvalidTransitionError
        evidence = StepEvidence(
            label=label,
            snapshot_succeeded=True,
            baseline_restored=(
                _snapshots_equal(after, baseline) if label.target.value == "off" else None
            ),
            changes=diff_snapshots(before, after, tuple(self._step_transient)),
        )
        self._steps.append(evidence)
        self._clear_step()
        self._state = DiscoveryState.READY
        return evidence

    async def async_finish(self) -> JsonObject:
        """Validate and atomically save the report before clearing the session."""
        self._require_state(DiscoveryState.READY)
        started_at = self._started_at
        if started_at is None:
            self._cleanup(DiscoveryInvalidTransitionError.error_code)
            raise DiscoveryInvalidTransitionError
        self._state = DiscoveryState.FINALIZING
        report = build_discovery_report(
            integration_version=self._integration_version,
            started_at=started_at,
            wss_baseline_succeeded=self._baseline is not None,
            steps=tuple(self._steps),
        )
        try:
            self._validate_report(report)
            await self._save_report(report)
        except asyncio.CancelledError:
            self._cleanup()
            raise
        except Exception:  # noqa: BLE001 - hide validator and Store failures
            self._cleanup(DiscoveryReportSaveFailedError.error_code)
            raise DiscoveryReportSaveFailedError from None
        self._cleanup()
        return report

    async def async_cancel(self) -> None:
        """Cancel any active session without replacing the prior report."""
        if self._state is DiscoveryState.IDLE:
            raise DiscoveryInvalidTransitionError
        self._cleanup()

    async def async_stop(self) -> None:
        """Idempotently cancel discovery and await all owned timer tasks."""
        if self._state is not DiscoveryState.IDLE:
            self.cancel_from_transport(DiscoveryWssUnavailableError.error_code)
        current = asyncio.current_task()
        pending = tuple(task for task in self._owned_tasks if task is not current)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._owned_tasks.clear()

    def async_observe_shadow(self, message: AcceptedShadow) -> None:
        """Consume only correlated get snapshots and bounded update observations."""
        future = self._pending_future
        if (
            future is not None
            and not future.done()
            and message.topic_kind == "get"
            and message.client_token == self._pending_token
        ):
            try:
                sanitizer = self._require_sanitizer()
                if not sanitizer.has_target_reported(message.state):
                    raise DiscoverySanitizationError
                future.set_result(sanitizer.sanitize_reported(message.state))
            except DiscoverySanitizationError:
                future.set_exception(DiscoverySnapshotTimeoutError())
            return

        if self._state is not DiscoveryState.OBSERVING or message.topic_kind != "update":
            return
        if self._step_invalid:
            return
        try:
            observed = self._require_sanitizer().sanitize_reported(message.state)
        except DiscoverySanitizationError:
            self._invalidate_step()
            return
        last_values = self._step_last_values
        if last_values is None:
            self._invalidate_step()
            return
        for path, value in sorted(observed.items()):
            previous = last_values.get(path)
            if _sanitized_equal(previous, value):
                continue
            self._step_transient.append((path, value))
            last_values[path] = value
            if len(self._step_transient) > self._max_changes_per_step:
                self._invalidate_step()
                return

    def cancel_from_transport(self, error_code: str) -> None:
        """Synchronously clear a session after disconnect, auth failure, or expiry."""
        if self._state is DiscoveryState.IDLE:
            return
        controlled_code = (
            error_code
            if error_code
            in {
                DiscoveryWssUnavailableError.error_code,
                DiscoveryStepExpiredError.error_code,
                DiscoverySessionExpiredError.error_code,
            }
            else DiscoveryWssUnavailableError.error_code
        )
        self._cleanup(controlled_code)

    async def _async_request_snapshot(self) -> Snapshot:
        if self._pending_future is not None:
            raise DiscoveryInvalidTransitionError
        loop = asyncio.get_running_loop()
        token = "disc-" + secrets.token_hex(16)
        future: asyncio.Future[Snapshot] = loop.create_future()
        self._pending_token = token
        self._pending_future = future
        try:
            try:
                await self._request_shadow_get(token)
            except asyncio.CancelledError:
                raise
            except DiscoveryError:
                raise
            except Exception:  # noqa: BLE001 - hide injected transport failures
                raise DiscoveryWssUnavailableError from None
            try:
                async with asyncio.timeout(self._snapshot_timeout_seconds):
                    return await future
            except TimeoutError:
                raise DiscoverySnapshotTimeoutError from None
        finally:
            if self._pending_future is future:
                self._pending_future = None
                self._pending_token = None

    def _require_state(self, expected: DiscoveryState) -> None:
        if self._state is not expected:
            raise DiscoveryInvalidTransitionError

    def _require_sanitizer(self) -> DiscoverySanitizer:
        if self._sanitizer is None:
            raise DiscoverySanitizationError
        return self._sanitizer

    def _invalidate_step(self) -> None:
        self._step_invalid = True
        self._step_before = None
        self._step_last_values = None
        self._step_transient.clear()

    def _clear_step(self) -> None:
        self._cancel_task(self._step_timer)
        self._step_timer = None
        self._step_label = None
        self._step_before = None
        self._step_last_values = None
        self._step_transient.clear()
        self._step_invalid = False

    def _cleanup(self, error_code: str | None = None) -> None:
        if self._state is not DiscoveryState.IDLE:
            self._state = DiscoveryState.CANCELLED
        if error_code is not None:
            self._last_error_code = error_code
            future = self._pending_future
            if future is not None and not future.done():
                future.set_exception(_error_for_code(error_code))
        self._pending_future = None
        self._pending_token = None
        self._cancel_task(self._step_timer)
        self._cancel_task(self._session_timer)
        self._step_timer = None
        self._session_timer = None
        self._step_label = None
        self._step_before = None
        self._step_last_values = None
        self._step_transient.clear()
        self._step_invalid = False
        self._baseline = None
        self._steps.clear()
        self._started_at = None
        self._sanitizer = None
        self._session_key = None
        if self._observer_active:
            self._observer_active = False
            self._deactivate_observer()
        self._state = DiscoveryState.IDLE

    def _create_expiry_task(
        self,
        delay: float,
        error_code: str,
        name: str,
    ) -> asyncio.Task[None]:
        async def expire() -> None:
            await asyncio.sleep(delay)
            self.cancel_from_transport(error_code)

        task = asyncio.create_task(expire(), name=name)
        self._owned_tasks.add(task)
        task.add_done_callback(self._owned_tasks.discard)
        return task

    @staticmethod
    def _cancel_task(task: asyncio.Task[None] | None) -> None:
        if task is not None and task is not asyncio.current_task():
            task.cancel()


def _error_for_code(error_code: str) -> DiscoveryError:
    errors: dict[str, type[DiscoveryError]] = {
        DiscoveryWssUnavailableError.error_code: DiscoveryWssUnavailableError,
        DiscoveryStepExpiredError.error_code: DiscoveryStepExpiredError,
        DiscoverySessionExpiredError.error_code: DiscoverySessionExpiredError,
    }
    return errors.get(error_code, DiscoveryWssUnavailableError)()


def _sanitized_equal(
    left: SanitizedValue | None,
    right: SanitizedValue | None,
) -> bool:
    if left is None or right is None:
        return left is right
    return left.kind == right.kind and left.comparison == right.comparison


def _snapshots_equal(left: Snapshot, right: Snapshot) -> bool:
    return left.keys() == right.keys() and all(
        _sanitized_equal(left[path], right[path]) for path in left
    )
