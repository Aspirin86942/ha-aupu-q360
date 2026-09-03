"""Bounded, read-only Q360 v2 panel discovery sessions."""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Awaitable, Callable, Collection, Mapping
from datetime import UTC, datetime
from typing import Protocol

from .discovery_analysis import (
    background_paths,
    build_discovery_report,
    confirmed_paths_for_experiment,
    diff_snapshots,
    evaluate_restoration,
)
from .discovery_catalog import PROMPT_CODE_BY_PHASE, build_step_request, definition_for
from .discovery_models import (
    CycleEvidence,
    DiscoveryPhase,
    DiscoveryProgress,
    DiscoveryState,
    DiscoveryStepRequest,
    JsonObject,
    PathRestoration,
    PhaseEvidence,
    SanitizedChange,
    SanitizedValue,
)
from .discovery_sanitizer import DiscoverySanitizationError, DiscoverySanitizer
from .errors import (
    DiscoveryBusyError,
    DiscoveryError,
    DiscoveryInvalidParameterError,
    DiscoveryInvalidPayloadError,
    DiscoveryInvalidTransitionError,
    DiscoveryManualRestoreRequiredError,
    DiscoveryRawArchiveFailedError,
    DiscoveryRawArchiveLimitError,
    DiscoveryReportSaveFailedError,
    DiscoveryResourceLimitError,
    DiscoverySessionExpiredError,
    DiscoverySnapshotTimeoutError,
    DiscoveryStepExpiredError,
    DiscoveryWssUnavailableError,
)
from .raw_discovery_archive import ArchiveContext, RawArchiveMetadata
from .shadow import AcceptedShadow, RawShadowEvent

type Snapshot = dict[str, SanitizedValue]
type OutgoingRecorder = Callable[[RawShadowEvent], None]
type SnapshotRequester = Callable[[str, OutgoingRecorder | None], Awaitable[None]]
type ReportSaver = Callable[[JsonObject], Awaitable[None]]
type SanitizerFactory = Callable[[bytes], DiscoverySanitizer]
type ReportValidator = Callable[[object], object]
type Observer = Callable[[AcceptedShadow], None]
type CancelCallback = Callable[[], None]
type ObserverActivator = Callable[[Observer, CancelCallback], None]
type Clock = Callable[[], datetime]
type TransportPreparer = Callable[[], Awaitable[None]]

_DEFAULT_SNAPSHOT_TIMEOUT = 10.0
_DEFAULT_STAGE_TIMEOUT = 300.0
_DEFAULT_SESSION_TIMEOUT = 3300.0
_DEFAULT_MAX_CHANGES = 256
_LOGGER = logging.getLogger(__name__)

_POSITIVE_PHASES = frozenset(
    {
        DiscoveryPhase.MODE_ON,
        DiscoveryPhase.CARRIER_ON,
        DiscoveryPhase.PARAMETER_CHANGE,
    }
)
_RESTORE_PHASES = frozenset(
    {
        DiscoveryPhase.MODE_RESTORE,
        DiscoveryPhase.PARAMETER_RESTORE,
        DiscoveryPhase.CARRIER_OFF,
    }
)


class DiscoveryArchive(Protocol):
    """Lifecycle surface supplied by the private raw archive writer."""

    @property
    def metadata(self) -> RawArchiveMetadata:
        """Return secret-free point-in-time metadata."""

    def enqueue(self, event: RawShadowEvent, context: ArchiveContext) -> None:
        """Queue one exact raw event without blocking."""

    async def async_complete(self) -> RawArchiveMetadata:
        """Complete the archive and return final metadata."""

    async def async_abort(self) -> RawArchiveMetadata:
        """Retain an incomplete archive."""

    async def async_stop(self) -> None:
        """Stop the archive writer idempotently."""


type ArchiveFactory = Callable[[Callable[[str], None]], Awaitable[DiscoveryArchive]]


def _utc_now() -> datetime:
    """Return the current aware UTC time."""
    return datetime.now(UTC)


class PanelStateDiscoverySession:
    """Own one catalog-driven, read-only panel discovery session."""

    def __init__(
        self,
        *,
        prepare_transport: TransportPreparer,
        request_shadow_get: SnapshotRequester,
        save_report: ReportSaver,
        sanitizer_factory: SanitizerFactory,
        validate_report: ReportValidator,
        activate_observer: ObserverActivator,
        deactivate_observer: Callable[[], None],
        discovery_available: Callable[[], bool],
        integration_version: str,
        archive_factory: ArchiveFactory | None = None,
        now: Clock = _utc_now,
        snapshot_timeout_seconds: float = _DEFAULT_SNAPSHOT_TIMEOUT,
        stage_timeout_seconds: float = _DEFAULT_STAGE_TIMEOUT,
        session_timeout_seconds: float = _DEFAULT_SESSION_TIMEOUT,
        max_changes_per_phase: int = _DEFAULT_MAX_CHANGES,
    ) -> None:
        self._prepare_transport = prepare_transport
        self._request_shadow_get = request_shadow_get
        self._save_report = save_report
        self._sanitizer_factory = sanitizer_factory
        self._validate_report = validate_report
        self._activate_observer = activate_observer
        self._deactivate_observer = deactivate_observer
        self._discovery_available = discovery_available
        self._integration_version = integration_version
        self._archive_factory = archive_factory
        self._now = now
        self._snapshot_timeout_seconds = snapshot_timeout_seconds
        self._stage_timeout_seconds = stage_timeout_seconds
        self._session_timeout_seconds = session_timeout_seconds
        self._max_changes_per_phase = max_changes_per_phase

        self._state = DiscoveryState.IDLE
        self._last_failure_code = "none"
        self._last_manual_restore_required = False
        self._observer_active = False
        self._session_key: bytes | None = None
        self._sanitizer: DiscoverySanitizer | None = None
        self._started_at: datetime | None = None
        self._session_baseline: Snapshot | None = None
        self._archive: DiscoveryArchive | None = None
        self._archive_metadata = RawArchiveMetadata.not_requested()

        self._pending_token: str | None = None
        self._pending_future: asyncio.Future[Snapshot] | None = None
        self._archive_context: ArchiveContext | None = None

        self._current_request: DiscoveryStepRequest | None = None
        self._current_phases: tuple[DiscoveryPhase, ...] = ()
        self._phase_index = 0
        self._phase_attempt = 1
        self._step_baseline: Snapshot | None = None
        self._carrier_baseline: Snapshot | None = None
        self._phase_before: Snapshot | None = None
        self._phase_last_values: Snapshot | None = None
        self._phase_transient: list[tuple[str, SanitizedValue]] = []
        self._cycle_phases: list[PhaseEvidence] = []
        self._completed_cycles: list[CycleEvidence] = []
        self._pending_restore_paths: set[str] = set()

        self._session_timer: asyncio.Task[None] | None = None
        self._stage_timer: asyncio.Task[None] | None = None
        self._owned_tasks: set[asyncio.Task[None]] = set()
        self._abort_task: asyncio.Task[None] | None = None
        self._cleanup_lock = asyncio.Lock()

    @property
    def state(self) -> DiscoveryState:
        """Return the current controlled state."""
        return self._state

    @property
    def completed_cycles(self) -> tuple[CycleEvidence, ...]:
        """Return immutable sanitized evidence for completed cycles."""
        return tuple(self._completed_cycles)

    @property
    def pending_token(self) -> str | None:
        """Return the pending token only for internal lifecycle verification."""
        return self._pending_token

    @property
    def last_failure_code(self) -> str:
        """Return only the latest fixed failure code."""
        return self._last_failure_code

    @property
    def last_manual_restore_required(self) -> bool:
        """Return the evidence-based restoration risk from the last stop."""
        return self._last_manual_restore_required

    async def async_start(
        self,
        all_modes_off_confirmed: bool,
    ) -> DiscoveryProgress:
        """Open optional archiving and establish the session baseline."""
        if self._state is not DiscoveryState.IDLE:
            raise DiscoveryBusyError
        if all_modes_off_confirmed is not True:
            raise DiscoveryInvalidParameterError
        self._last_failure_code = "none"
        self._last_manual_restore_required = False
        self._state = DiscoveryState.TRANSPORT_PREPARING
        try:
            try:
                await self._prepare_transport()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - hide transport dependency details
                raise DiscoveryWssUnavailableError from None
            if not self._discovery_available():
                raise DiscoveryWssUnavailableError

            self._state = DiscoveryState.ARCHIVE_OPENING
            self._session_key = secrets.token_bytes(32)
            self._sanitizer = self._sanitizer_factory(self._session_key)
            if self._archive_factory is not None:
                self._archive = await self._archive_factory(self.cancel_from_transport)
                self._archive_metadata = self._archive.metadata

            self._activate_observer(
                self.async_observe_shadow,
                lambda: self.cancel_from_transport(DiscoveryWssUnavailableError.error_code),
            )
            self._observer_active = True
            started_at = self._now()
            if started_at.tzinfo is None or started_at.utcoffset() is None:
                raise DiscoveryInvalidPayloadError
            self._started_at = started_at.astimezone(UTC)
            self._session_timer = self._create_expiry_task(
                self._session_timeout_seconds,
                DiscoverySessionExpiredError.error_code,
                "aupu_q360_discovery_session_timeout",
            )
            self._state = DiscoveryState.SESSION_BASELINING
            context = ArchiveContext(
                experiment="session",
                round=0,
                phase=DiscoveryPhase.SESSION_BASELINE,
            )
            self._session_baseline = await self._async_request_snapshot(context)
        except asyncio.CancelledError:
            await self._abort_after_external_cancellation()
            raise
        except DiscoveryError as err:
            await self._ensure_aborted(err.error_code)
            raise
        except Exception:  # noqa: BLE001 - hide injected dependency details
            await self._ensure_aborted(DiscoveryWssUnavailableError.error_code)
            raise DiscoveryWssUnavailableError from None

        self._state = DiscoveryState.READY
        return self._progress("discovery_ready_for_step")

    async def async_begin_step(
        self,
        request: DiscoveryStepRequest,
    ) -> DiscoveryProgress:
        """Validate one cycle, snapshot its baseline, and prompt its first phase."""
        self._require_state(DiscoveryState.READY)
        controlled = self._validate_step_request(request)

        self._current_request = controlled
        self._current_phases = definition_for(controlled.experiment).phases
        self._phase_index = 0
        self._phase_attempt = 1
        self._cycle_phases = []
        self._carrier_baseline = None
        self._state = DiscoveryState.STEP_BASELINING
        context = ArchiveContext(
            experiment=controlled.experiment,
            round=controlled.round,
            phase=DiscoveryPhase.STEP_BASELINE,
        )
        try:
            baseline = await self._async_request_snapshot(context)
        except asyncio.CancelledError:
            await self._abort_after_external_cancellation()
            raise
        except DiscoveryError as err:
            await self._ensure_aborted(err.error_code)
            raise

        self._step_baseline = baseline
        self._phase_before = baseline
        self._reset_phase_observation(baseline)
        self._state = DiscoveryState.AWAITING_OPERATOR
        self._reset_stage_timer()
        return self._phase_progress()

    async def async_advance_step(self) -> DiscoveryProgress:
        """Snapshot the current catalog phase and advance or request restoration."""
        if self._state not in {
            DiscoveryState.AWAITING_OPERATOR,
            DiscoveryState.RESTORE_REQUIRED,
        }:
            raise DiscoveryInvalidTransitionError
        if self._pending_future is not None:
            raise DiscoveryInvalidTransitionError
        request = self._current_request
        phase = self._current_phase()
        before = self._phase_before
        if request is None or before is None:
            await self._ensure_aborted(DiscoveryInvalidTransitionError.error_code)
            raise DiscoveryInvalidTransitionError

        context = ArchiveContext(
            experiment=request.experiment,
            round=request.round,
            phase=phase,
        )
        try:
            snapshot = await self._async_request_snapshot(context)
            changes = diff_snapshots(before, snapshot, tuple(self._phase_transient))
            if len(changes) > self._max_changes_per_phase:
                raise DiscoveryResourceLimitError
            evidence, restoration_required = self._build_phase_evidence(
                phase=phase,
                snapshot=snapshot,
                changes=changes,
            )
            self._cycle_phases.append(evidence)
        except asyncio.CancelledError:
            await self._abort_after_external_cancellation()
            raise
        except DiscoveryError as err:
            await self._ensure_aborted(err.error_code)
            raise

        if restoration_required:
            self._phase_attempt += 1
            self._state = DiscoveryState.RESTORE_REQUIRED
            self._reset_phase_observation(before)
            self._reset_stage_timer()
            return DiscoveryProgress(
                state=DiscoveryState.RESTORE_REQUIRED,
                message_code="discovery_restore_required",
                phase=phase,
                completed_cycle_count=len(self._completed_cycles),
                manual_restore_required=True,
            )

        self._phase_before = snapshot
        if phase is DiscoveryPhase.CARRIER_ON:
            self._carrier_baseline = snapshot
        self._phase_index += 1
        self._phase_attempt = 1
        if self._phase_index == len(self._current_phases):
            self._completed_cycles.append(
                CycleEvidence(
                    request=request,
                    phases=tuple(self._cycle_phases),
                )
            )
            self._clear_current_cycle()
            self._cancel_task(self._stage_timer)
            self._stage_timer = None
            self._state = DiscoveryState.READY
            return self._progress(
                "discovery_cycle_recorded",
                manual_restore_required=self._manual_restore_required(),
            )

        self._state = DiscoveryState.AWAITING_OPERATOR
        self._reset_phase_observation(snapshot)
        self._reset_stage_timer()
        return self._phase_progress()

    async def async_finish(self) -> JsonObject:
        """Complete raw evidence, validate schema v2, and atomically save the report."""
        self._require_state(DiscoveryState.READY)
        if self._manual_restore_required():
            raise DiscoveryManualRestoreRequiredError
        started_at = self._started_at
        if started_at is None:
            await self._ensure_aborted(DiscoveryInvalidTransitionError.error_code)
            raise DiscoveryInvalidTransitionError

        self._state = DiscoveryState.FINALIZING
        archive = self._archive
        try:
            if archive is not None:
                self._archive_metadata = await archive.async_complete()
            report = build_discovery_report(
                integration_version=self._integration_version,
                started_at=started_at,
                wss_baseline_succeeded=self._session_baseline is not None,
                archive=self._archive_metadata,
                cycles=tuple(self._completed_cycles),
            )
            self._validate_report(report)
            await self._save_report(report)
        except asyncio.CancelledError:
            await self._abort_after_external_cancellation()
            raise
        except (DiscoveryRawArchiveFailedError, DiscoveryRawArchiveLimitError) as err:
            await self._ensure_aborted(err.error_code)
            raise
        except Exception:  # noqa: BLE001 - never expose validator or Store details
            await self._ensure_aborted(DiscoveryReportSaveFailedError.error_code)
            raise DiscoveryReportSaveFailedError from None

        await self._end_session()
        return report

    async def async_cancel(self) -> DiscoveryProgress:
        """Cancel active collection without sending any panel command."""
        if self._state is DiscoveryState.IDLE:
            raise DiscoveryInvalidTransitionError
        manual_restore_required = self._manual_restore_required()
        message_code = (
            "discovery_manual_restore_required"
            if manual_restore_required
            else "discovery_cancelled"
        )
        await self._ensure_aborted(None)
        return DiscoveryProgress(
            state=DiscoveryState.CANCELLED,
            message_code=message_code,
            completed_cycle_count=0,
            manual_restore_required=manual_restore_required,
        )

    async def async_stop(self) -> None:
        """Idempotently stop timers, observer, sanitizer, and optional writer."""
        if self._state is not DiscoveryState.IDLE:
            self.cancel_from_transport(DiscoveryWssUnavailableError.error_code)
        abort_task = self._abort_task
        if abort_task is not None and abort_task is not asyncio.current_task():
            await asyncio.shield(abort_task)
        current = asyncio.current_task()
        pending = tuple(task for task in self._owned_tasks if task is not current)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._owned_tasks.clear()

    def async_observe_shadow(self, message: AcceptedShadow) -> None:
        """Archive first, then consume reported-only correlated/transient evidence."""
        archive = self._archive
        context = self._archive_context
        if archive is not None and context is not None:
            try:
                archive.enqueue(message.raw_event, context)
            except (DiscoveryRawArchiveFailedError, DiscoveryRawArchiveLimitError) as err:
                self.cancel_from_transport(err.error_code)
                return
            except Exception:  # noqa: BLE001 - archive content must never escape
                self.cancel_from_transport(DiscoveryRawArchiveFailedError.error_code)
                return

        if "reported" not in message.state:
            return

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
                    return
                future.set_result(sanitizer.sanitize_reported(message.state))
            except DiscoverySanitizationError:
                future.set_exception(DiscoveryInvalidPayloadError())
            return

        if message.topic_kind != "update" or self._state not in {
            DiscoveryState.AWAITING_OPERATOR,
            DiscoveryState.RESTORE_REQUIRED,
        }:
            return
        try:
            observed = self._require_sanitizer().sanitize_reported(message.state)
        except DiscoverySanitizationError:
            self.cancel_from_transport(DiscoveryInvalidPayloadError.error_code)
            return
        last_values = self._phase_last_values
        if last_values is None:
            self.cancel_from_transport(DiscoveryInvalidTransitionError.error_code)
            return
        for path, value in sorted(observed.items()):
            previous = last_values.get(path)
            if _sanitized_equal(previous, value):
                continue
            self._phase_transient.append((path, value))
            last_values[path] = value
            if len(self._phase_transient) > self._max_changes_per_phase:
                self.cancel_from_transport(DiscoveryResourceLimitError.error_code)
                return

    def cancel_from_transport(
        self,
        error_code: str = DiscoveryWssUnavailableError.error_code,
    ) -> None:
        """Schedule one fail-closed cleanup path from a synchronous callback."""
        if self._state is DiscoveryState.IDLE:
            return
        controlled = _controlled_error_code(error_code)
        self._last_failure_code = controlled
        self._last_manual_restore_required = self._manual_restore_required()
        future = self._pending_future
        if future is not None and not future.done():
            future.set_exception(_error_for_code(controlled))
        task = self._abort_task
        if task is None or task.done():
            task = asyncio.create_task(
                self._async_abort_session(controlled),
                name="aupu_q360_discovery_abort",
            )
            self._abort_task = task

    def _validate_step_request(
        self,
        request: DiscoveryStepRequest,
    ) -> DiscoveryStepRequest:
        if not isinstance(request, DiscoveryStepRequest):
            raise DiscoveryInvalidParameterError
        try:
            controlled = build_step_request(
                experiment=request.experiment.value,
                round_number=request.round,
                source_level=request.source_level,
                target_level=request.target_level,
                source_temperature=request.source_temperature,
                target_temperature=request.target_temperature,
            )
        except (AttributeError, TypeError, ValueError):
            raise DiscoveryInvalidParameterError from None
        if controlled != request:
            raise DiscoveryInvalidParameterError

        if any(cycle.request.cycle_id == controlled.cycle_id for cycle in self._completed_cycles):
            raise DiscoveryInvalidParameterError
        fan_sources = {
            cycle.request.source_level
            for cycle in self._completed_cycles
            if cycle.request.experiment.value == "global_fan_level"
        }
        if (
            controlled.experiment.value == "global_fan_level"
            and fan_sources
            and controlled.source_level not in fan_sources
        ):
            raise DiscoveryInvalidParameterError
        temperature_pairs = {
            (
                cycle.request.source_temperature,
                cycle.request.target_temperature,
            )
            for cycle in self._completed_cycles
            if cycle.request.experiment.value == "ai_target_temperature"
        }
        if (
            controlled.experiment.value == "ai_target_temperature"
            and temperature_pairs
            and (
                controlled.source_temperature,
                controlled.target_temperature,
            )
            not in temperature_pairs
        ):
            raise DiscoveryInvalidParameterError
        return controlled

    async def _async_request_snapshot(self, context: ArchiveContext) -> Snapshot:
        if self._pending_future is not None:
            raise DiscoveryInvalidTransitionError
        loop = asyncio.get_running_loop()
        token = "disc-" + secrets.token_hex(16)
        future: asyncio.Future[Snapshot] = loop.create_future()
        self._pending_token = token
        self._pending_future = future
        self._archive_context = context
        request_completed = False

        recorder: OutgoingRecorder | None = None
        archive = self._archive
        if archive is not None:

            def record_outgoing(event: RawShadowEvent) -> None:
                archive.enqueue(event, context)

            recorder = record_outgoing

        try:
            try:
                await self._request_shadow_get(token, recorder)
                request_completed = True
            except asyncio.CancelledError:
                raise
            except DiscoveryError:
                raise
            except Exception:  # noqa: BLE001 - hide transport details
                raise DiscoveryWssUnavailableError from None
            try:
                async with asyncio.timeout(self._snapshot_timeout_seconds):
                    return await future
            except TimeoutError:
                raise DiscoverySnapshotTimeoutError from None
        finally:
            if not future.done():
                future.cancel()
            elif not request_completed:
                try:
                    future.exception()
                except asyncio.CancelledError:
                    pass
            if self._pending_future is future:
                self._pending_future = None
                self._pending_token = None

    def _build_phase_evidence(
        self,
        *,
        phase: DiscoveryPhase,
        snapshot: Snapshot,
        changes: tuple[SanitizedChange, ...],
    ) -> tuple[PhaseEvidence, bool]:
        restorations: tuple[PathRestoration, ...] = ()
        restoration_required = False
        if phase in _RESTORE_PHASES:
            reference, positive_changes = self._restoration_inputs(phase)
            request = self._current_request
            if request is None:
                raise DiscoveryInvalidTransitionError
            result = evaluate_restoration(
                reference=reference,
                positive_changes=positive_changes,
                candidate=snapshot,
                background=background_paths(self._completed_cycles),
                confirmed_paths=confirmed_paths_for_experiment(
                    self._completed_cycles,
                    request.experiment,
                ),
            )
            restorations = result.restorations
            self._pending_restore_paths.difference_update(result.restored_paths)
            restoration_required = result.required
        elif phase in _POSITIVE_PHASES:
            self._pending_restore_paths.update(_restorable_paths(changes))

        return (
            PhaseEvidence(
                phase=phase,
                attempt=self._phase_attempt,
                snapshot_succeeded=True,
                changes=changes,
                restorations=restorations,
            ),
            restoration_required,
        )

    def _restoration_inputs(
        self,
        phase: DiscoveryPhase,
    ) -> tuple[Snapshot, tuple[SanitizedChange, ...]]:
        if phase is DiscoveryPhase.MODE_RESTORE:
            reference = self._step_baseline
            positive_phase = DiscoveryPhase.MODE_ON
        elif phase is DiscoveryPhase.PARAMETER_RESTORE:
            reference = self._carrier_baseline
            positive_phase = DiscoveryPhase.PARAMETER_CHANGE
        elif phase is DiscoveryPhase.CARRIER_OFF:
            reference = self._step_baseline
            positive_phase = DiscoveryPhase.CARRIER_ON
        else:
            raise DiscoveryInvalidTransitionError
        if reference is None:
            raise DiscoveryInvalidTransitionError
        positive = next(
            (
                evidence.changes
                for evidence in reversed(self._cycle_phases)
                if evidence.phase is positive_phase
            ),
            None,
        )
        if positive is None:
            raise DiscoveryInvalidTransitionError
        return reference, positive

    def _current_phase(self) -> DiscoveryPhase:
        if not 0 <= self._phase_index < len(self._current_phases):
            raise DiscoveryInvalidTransitionError
        return self._current_phases[self._phase_index]

    def _phase_progress(self) -> DiscoveryProgress:
        phase = self._current_phase()
        return DiscoveryProgress(
            state=DiscoveryState.AWAITING_OPERATOR,
            message_code=PROMPT_CODE_BY_PHASE[phase],
            phase=phase,
            completed_cycle_count=len(self._completed_cycles),
            manual_restore_required=self._manual_restore_required(),
        )

    def _progress(
        self,
        message_code: str,
        *,
        manual_restore_required: bool = False,
    ) -> DiscoveryProgress:
        return DiscoveryProgress(
            state=self._state,
            message_code=message_code,
            completed_cycle_count=len(self._completed_cycles),
            manual_restore_required=manual_restore_required,
        )

    def _reset_phase_observation(self, reference: Mapping[str, SanitizedValue]) -> None:
        self._phase_transient = []
        self._phase_last_values = dict(reference)
        request = self._current_request
        if request is not None and self._current_phases:
            self._archive_context = ArchiveContext(
                experiment=request.experiment,
                round=request.round,
                phase=self._current_phase(),
            )

    def _reset_stage_timer(self) -> None:
        self._cancel_task(self._stage_timer)
        self._stage_timer = self._create_expiry_task(
            self._stage_timeout_seconds,
            DiscoveryStepExpiredError.error_code,
            "aupu_q360_discovery_stage_timeout",
        )

    def _clear_current_cycle(self) -> None:
        self._current_request = None
        self._current_phases = ()
        self._phase_index = 0
        self._phase_attempt = 1
        self._step_baseline = None
        self._carrier_baseline = None
        self._phase_before = None
        self._phase_last_values = None
        self._phase_transient.clear()
        self._cycle_phases = []
        self._archive_context = None

    def _manual_restore_required(self) -> bool:
        return bool(self._pending_restore_paths)

    def _require_state(self, expected: DiscoveryState) -> None:
        if self._state is not expected:
            raise DiscoveryInvalidTransitionError

    def _require_sanitizer(self) -> DiscoverySanitizer:
        sanitizer = self._sanitizer
        if sanitizer is None:
            raise DiscoverySanitizationError
        return sanitizer

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

    async def _ensure_aborted(self, error_code: str | None) -> None:
        task = self._abort_task
        if task is None or task.done():
            task = asyncio.create_task(
                self._async_abort_session(error_code),
                name="aupu_q360_discovery_abort",
            )
            self._abort_task = task
        await asyncio.shield(task)

    async def _abort_after_external_cancellation(self) -> None:
        task = asyncio.create_task(
            self._async_abort_session(None),
            name="aupu_q360_discovery_cancel_cleanup",
        )
        await asyncio.shield(task)

    async def _async_abort_session(self, error_code: str | None) -> None:
        async with self._cleanup_lock:
            if error_code is not None:
                self._last_failure_code = _controlled_error_code(error_code)
            self._last_manual_restore_required = self._manual_restore_required()
            if self._state is not DiscoveryState.IDLE:
                self._state = DiscoveryState.CANCELLED
            await self._release_resources(abort_archive=True)

    async def _end_session(self) -> None:
        async with self._cleanup_lock:
            self._last_manual_restore_required = False
            await self._release_resources(abort_archive=False)

    async def _release_resources(self, *, abort_archive: bool) -> None:
        future = self._pending_future
        if future is not None and not future.done():
            future.cancel()
        self._pending_future = None
        self._pending_token = None

        self._cancel_task(self._stage_timer)
        self._cancel_task(self._session_timer)
        self._stage_timer = None
        self._session_timer = None

        if self._observer_active:
            self._observer_active = False
            self._deactivate_observer()

        archive = self._archive
        self._archive = None
        if archive is not None:
            if abort_archive and archive.metadata.status == "open":
                try:
                    self._archive_metadata = await archive.async_abort()
                except Exception:  # noqa: BLE001 - preserve fixed cleanup boundary
                    _LOGGER.error("AUPU discovery archive abort failed")
            try:
                await archive.async_stop()
            except Exception:  # noqa: BLE001 - preserve fixed cleanup boundary
                _LOGGER.error("AUPU discovery archive stop failed")

        sanitizer = self._sanitizer
        self._sanitizer = None
        if sanitizer is not None:
            sanitizer.close()
        self._session_key = None
        self._started_at = None
        self._session_baseline = None
        self._archive_context = None
        self._archive_metadata = RawArchiveMetadata.not_requested()
        self._clear_current_cycle()
        self._completed_cycles.clear()
        self._pending_restore_paths.clear()
        self._state = DiscoveryState.IDLE

    @staticmethod
    def _cancel_task(task: asyncio.Task[None] | None) -> None:
        if task is not None and task is not asyncio.current_task():
            task.cancel()


def _restorable_paths(changes: Collection[SanitizedChange]) -> set[str]:
    return {change.path for change in changes if change.data_type != "timestamp"}


def _sanitized_equal(
    left: SanitizedValue | None,
    right: SanitizedValue | None,
) -> bool:
    if left is None or right is None:
        return left is right
    return left.kind == right.kind and left.comparison == right.comparison


def _controlled_error_code(error_code: str) -> str:
    controlled = {
        DiscoveryInvalidPayloadError.error_code,
        DiscoveryInvalidTransitionError.error_code,
        DiscoveryRawArchiveFailedError.error_code,
        DiscoveryRawArchiveLimitError.error_code,
        DiscoveryReportSaveFailedError.error_code,
        DiscoveryResourceLimitError.error_code,
        DiscoverySessionExpiredError.error_code,
        DiscoverySnapshotTimeoutError.error_code,
        DiscoveryStepExpiredError.error_code,
        DiscoveryWssUnavailableError.error_code,
    }
    return error_code if error_code in controlled else DiscoveryWssUnavailableError.error_code


def _error_for_code(error_code: str) -> DiscoveryError:
    error_types: dict[str, type[DiscoveryError]] = {
        DiscoveryInvalidPayloadError.error_code: DiscoveryInvalidPayloadError,
        DiscoveryInvalidTransitionError.error_code: DiscoveryInvalidTransitionError,
        DiscoveryRawArchiveFailedError.error_code: DiscoveryRawArchiveFailedError,
        DiscoveryRawArchiveLimitError.error_code: DiscoveryRawArchiveLimitError,
        DiscoveryReportSaveFailedError.error_code: DiscoveryReportSaveFailedError,
        DiscoveryResourceLimitError.error_code: DiscoveryResourceLimitError,
        DiscoverySessionExpiredError.error_code: DiscoverySessionExpiredError,
        DiscoverySnapshotTimeoutError.error_code: DiscoverySnapshotTimeoutError,
        DiscoveryStepExpiredError.error_code: DiscoveryStepExpiredError,
        DiscoveryWssUnavailableError.error_code: DiscoveryWssUnavailableError,
    }
    return error_types.get(error_code, DiscoveryWssUnavailableError)()
