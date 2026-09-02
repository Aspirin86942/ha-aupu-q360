"""Secret-free value and state models for Q360 read-only discovery."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

type JsonValue = str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]
type ValueKind = Literal["boolean", "number", "null", "string", "timestamp", "object", "array"]
type ComparisonValue = bool | int | float | str | None | tuple[str, int, int]
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
    BASELINING = "baselining"
    READY = "ready"
    STEP_BASELINING = "step_baselining"
    AWAITING_OPERATOR = "awaiting_operator"
    RESTORE_REQUIRED = "restore_required"
    OBSERVING = "observing"
    STEP_FINALIZING = "step_finalizing"
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


class DiscoveryCapability(StrEnum):
    """Controlled experiment capability labels."""

    HEATING = "heating"
    VENTILATION = "ventilation"
    DRYING = "drying"
    SWING = "swing"
    FAN_LEVEL = "fan_level"
    TIMER = "timer"
    IDLE_ENVIRONMENT = "idle_environment"


class DiscoveryTarget(StrEnum):
    """Controlled target labels accepted by discovery actions."""

    OFF = "off"
    ON = "on"
    LEVEL_1 = "level_1"
    LEVEL_2 = "level_2"
    LEVEL_3 = "level_3"


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
class StepLabel:
    """Controlled experiment label suitable for stable report identifiers."""

    capability: DiscoveryCapability
    target: DiscoveryTarget
    round: DiscoveryRound

    def __init__(
        self,
        capability: DiscoveryCapability | str,
        target: DiscoveryTarget | str,
        round: int,
    ) -> None:
        object.__setattr__(self, "capability", DiscoveryCapability(capability))
        object.__setattr__(self, "target", DiscoveryTarget(target))
        if round not in (1, 2):
            raise ValueError("invalid discovery round")
        object.__setattr__(self, "round", round)

    @property
    def evidence_id(self) -> str:
        """Return the fixed, secret-free identifier used in candidate evidence."""
        return f"{self.capability.value}:{self.target.value}:{self.round}"


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
class StepEvidence:
    """Bounded, sanitized evidence retained after one experiment step."""

    label: StepLabel
    snapshot_succeeded: bool
    baseline_restored: bool | None
    changes: tuple[SanitizedChange, ...]
    invalid: bool = False
    timed_out: bool = False

    def to_public(self) -> JsonObject:
        """Serialize only controlled labels and sanitized changes."""
        return {
            "capability": self.label.capability.value,
            "target": self.label.target.value,
            "round": self.label.round,
            "snapshot_succeeded": self.snapshot_succeeded,
            "baseline_restored": self.baseline_restored,
            "invalid": self.invalid,
            "timed_out": self.timed_out,
            "changes": [change.to_public() for change in self.changes],
        }
