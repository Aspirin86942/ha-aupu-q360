"""Secret-free value and state models for Q360 read-only discovery."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

type JsonValue = str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]
type ValueKind = Literal["boolean", "number", "null", "string", "timestamp", "object", "array"]
type ComparisonValue = bool | int | float | None | tuple[str, str]
type DiscoveryRound = Literal[1, 2]
type ChangeDirection = Literal[
    "added",
    "removed",
    "increase",
    "decrease",
    "off_to_on",
    "on_to_off",
    "changed",
]
type CandidateClassification = Literal[
    "confirmed_candidate",
    "ambiguous",
    "observed_unidentified",
    "not_observed",
    "invalid",
]


class DiscoveryState(StrEnum):
    """Internal state machine states that never contain user input."""

    IDLE = "idle"
    ARCHIVE_OPENING = "archive_opening"
    SESSION_BASELINING = "session_baselining"
    READY = "ready"
    STEP_BASELINING = "step_baselining"
    AWAITING_OPERATOR = "awaiting_operator"
    RESTORE_REQUIRED = "restore_required"
    FINALIZING = "finalizing"
    CANCELLED = "cancelled"


class DiscoveryExperiment(StrEnum):
    """Controlled v2 panel experiment labels."""

    AI_THERMOSTATIC_WARMTH = "ai_thermostatic_warmth"
    DEODORIZATION_STERILIZATION = "deodorization_sterilization"
    VENTILATION = "ventilation"
    AIR_BLOWING = "air_blowing"
    NORMAL_DRYING = "normal_drying"
    THERMOSTATIC_DRYING = "thermostatic_drying"
    NIGHT_LIGHT = "night_light"
    GLOBAL_FAN_LEVEL = "global_fan_level"
    AI_TARGET_TEMPERATURE = "ai_target_temperature"
    IDLE_ENVIRONMENT = "idle_environment"


class ExperimentKind(StrEnum):
    """Fixed workflow family for one discovery experiment."""

    MODE = "mode"
    PARAMETER = "parameter"
    IDLE = "idle"


class DiscoveryPhase(StrEnum):
    """Controlled evidence phases used by the v2 state machine."""

    SESSION_BASELINE = "session_baseline"
    STEP_BASELINE = "step_baseline"
    MODE_ON = "mode_on"
    MODE_RESTORE = "mode_restore"
    CARRIER_ON = "carrier_on"
    PARAMETER_CHANGE = "parameter_change"
    PARAMETER_RESTORE = "parameter_restore"
    CARRIER_OFF = "carrier_off"
    IDLE_OBSERVATION = "idle_observation"


class DiscoveryCoverage(StrEnum):
    """Coverage state for one fixed experiment."""

    NOT_STARTED = "not_started"
    PARTIAL = "partial"
    COMPLETE = "complete"


@dataclass(frozen=True, slots=True)
class DiscoveryStepRequest:
    """One validated, secret-free v2 experiment cycle request."""

    experiment: DiscoveryExperiment
    round: DiscoveryRound
    source_level: int | None = None
    target_level: int | None = None
    source_temperature: int | None = None
    target_temperature: int | None = None

    @property
    def cycle_id(self) -> str:
        """Return the stable controlled identifier for this cycle."""
        values = [self.experiment.value]
        if self.experiment is DiscoveryExperiment.GLOBAL_FAN_LEVEL:
            values.extend((str(self.source_level), str(self.target_level)))
        elif self.experiment is DiscoveryExperiment.AI_TARGET_TEMPERATURE:
            values.extend((str(self.source_temperature), str(self.target_temperature)))
        values.append(str(self.round))
        return ":".join(values)


@dataclass(frozen=True, slots=True)
class DiscoveryProgress:
    """Fixed public progress returned by v2 discovery actions."""

    state: DiscoveryState
    message_code: str
    phase: DiscoveryPhase | None = None
    completed_cycle_count: int = 0
    manual_restore_required: bool = False

    def to_response(self) -> JsonObject:
        """Serialize only the fixed action-response surface."""
        response: JsonObject = {
            "state": self.state.value,
            "message_code": self.message_code,
            "completed_cycle_count": self.completed_cycle_count,
            "manual_restore_required": self.manual_restore_required,
        }
        if self.phase is not None:
            response["phase"] = self.phase.value
        return response


@dataclass(frozen=True, slots=True)
class SanitizedValue:
    """One comparable in-memory value with a separately safe public form."""

    kind: ValueKind
    comparison: ComparisonValue = field(repr=False)
    public: JsonObject


@dataclass(frozen=True, slots=True)
class ScanResult:
    """Fixed result of the final report safety scan."""

    passed: bool
    finding_count: int


@dataclass(frozen=True, slots=True)
class SanitizedChange:
    """One aliased change whose comparisons never enter repr or serialization."""

    path: str
    data_type: ValueKind
    direction: ChangeDirection
    before: SanitizedValue | None = field(repr=False)
    after: SanitizedValue | None = field(repr=False)
    transient_count: int

    @property
    def correlation_signature(self) -> tuple[object, ...]:
        """Return an in-session signature used only to compare repeated rounds."""
        return (
            self.data_type,
            self.direction,
            None if self.before is None else self.before.kind,
            None if self.before is None else self.before.comparison,
            None if self.after is None else self.after.kind,
            None if self.after is None else self.after.comparison,
        )

    def to_public(self) -> JsonObject:
        """Serialize the change without raw timestamp comparisons."""
        public: JsonObject = {
            "path": self.path,
            "data_type": self.data_type,
            "direction": self.direction,
        }
        if (
            self.data_type == "timestamp"
            and self.before is not None
            and self.after is not None
            and isinstance(self.before.comparison, (int, float))
            and isinstance(self.after.comparison, (int, float))
        ):
            public["delta"] = self.after.comparison - self.before.comparison
        elif self.data_type != "timestamp":
            public["before"] = None if self.before is None else dict(self.before.public)
            public["after"] = None if self.after is None else dict(self.after.public)
        public["transient_count"] = self.transient_count
        return public


@dataclass(frozen=True, slots=True)
class PathRestoration:
    """One path-level restoration result without retaining compared values."""

    path: str
    restored: bool

    def to_public(self) -> JsonObject:
        """Serialize only the aliased path and restoration boolean."""
        return {"path": self.path, "restored": self.restored}


@dataclass(frozen=True, slots=True)
class PhaseEvidence:
    """Bounded sanitized evidence for one v2 experiment phase attempt."""

    phase: DiscoveryPhase
    attempt: int
    snapshot_succeeded: bool
    changes: tuple[SanitizedChange, ...]
    restorations: tuple[PathRestoration, ...] = ()
    invalid: bool = False
    timed_out: bool = False

    def to_public(self) -> JsonObject:
        """Serialize controlled phase data and sanitized evidence only."""
        return {
            "phase": self.phase.value,
            "attempt": self.attempt,
            "snapshot_succeeded": self.snapshot_succeeded,
            "invalid": self.invalid,
            "timed_out": self.timed_out,
            "changes": [change.to_public() for change in self.changes],
            "restorations": [
                restoration.to_public()
                for restoration in sorted(self.restorations, key=lambda item: item.path)
            ],
        }


@dataclass(frozen=True, slots=True)
class CycleEvidence:
    """All sanitized phase evidence retained for one controlled experiment cycle."""

    request: DiscoveryStepRequest
    phases: tuple[PhaseEvidence, ...]
    completed: bool = True
    invalid: bool = False

    @property
    def timed_out(self) -> bool:
        """Return whether any phase timed out."""
        return any(phase.timed_out for phase in self.phases)

    @property
    def restoration_failure_count(self) -> int:
        """Count path restoration checks that did not recover their references."""
        return sum(
            not restoration.restored for phase in self.phases for restoration in phase.restorations
        )

    def to_public(self) -> JsonObject:
        """Serialize one stable cycle row without in-memory comparison values."""
        public: JsonObject = {
            "cycle_id": self.request.cycle_id,
            "experiment": self.request.experiment.value,
            "round": self.request.round,
            "completed": self.completed,
            "invalid": self.invalid,
            "phases": [phase.to_public() for phase in self.phases],
        }
        for key, value in (
            ("source_level", self.request.source_level),
            ("target_level", self.request.target_level),
            ("source_temperature", self.request.source_temperature),
            ("target_temperature", self.request.target_temperature),
        ):
            if value is not None:
                public[key] = value
        return public


@dataclass(frozen=True, slots=True)
class RestorationResult:
    """In-memory result of one path-level restoration evaluation."""

    restorations: tuple[PathRestoration, ...]
    restored_paths: frozenset[str]
    unrestored_paths: frozenset[str]
    required: bool
