"""Fixed experiment catalog for Q360 panel state discovery v2."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import cast

from .discovery_models import (
    DiscoveryExperiment,
    DiscoveryPhase,
    DiscoveryRound,
    DiscoveryStepRequest,
    ExperimentKind,
)

GLOBAL_FAN_LEVELS = (1, 2, 3, 4, 5)
AI_TARGET_TEMPERATURES = tuple(range(30, 43))

MODE_EXPERIMENTS = (
    DiscoveryExperiment.AI_THERMOSTATIC_WARMTH,
    DiscoveryExperiment.DEODORIZATION_STERILIZATION,
    DiscoveryExperiment.VENTILATION,
    DiscoveryExperiment.AIR_BLOWING,
    DiscoveryExperiment.NORMAL_DRYING,
    DiscoveryExperiment.THERMOSTATIC_DRYING,
    DiscoveryExperiment.NIGHT_LIGHT,
)

_MODE_PHASES = (DiscoveryPhase.MODE_ON, DiscoveryPhase.MODE_RESTORE)
_PARAMETER_PHASES = (
    DiscoveryPhase.CARRIER_ON,
    DiscoveryPhase.PARAMETER_CHANGE,
    DiscoveryPhase.PARAMETER_RESTORE,
    DiscoveryPhase.CARRIER_OFF,
)


@dataclass(frozen=True, slots=True)
class ExperimentDefinition:
    """Immutable workflow definition for one controlled experiment."""

    experiment: DiscoveryExperiment
    kind: ExperimentKind
    carrier: DiscoveryExperiment | None
    phases: tuple[DiscoveryPhase, ...]


EXPERIMENT_CATALOG = MappingProxyType(
    {
        **{
            experiment: ExperimentDefinition(
                experiment=experiment,
                kind=ExperimentKind.MODE,
                carrier=None,
                phases=_MODE_PHASES,
            )
            for experiment in MODE_EXPERIMENTS
        },
        DiscoveryExperiment.GLOBAL_FAN_LEVEL: ExperimentDefinition(
            experiment=DiscoveryExperiment.GLOBAL_FAN_LEVEL,
            kind=ExperimentKind.PARAMETER,
            carrier=DiscoveryExperiment.VENTILATION,
            phases=_PARAMETER_PHASES,
        ),
        DiscoveryExperiment.AI_TARGET_TEMPERATURE: ExperimentDefinition(
            experiment=DiscoveryExperiment.AI_TARGET_TEMPERATURE,
            kind=ExperimentKind.PARAMETER,
            carrier=DiscoveryExperiment.AI_THERMOSTATIC_WARMTH,
            phases=_PARAMETER_PHASES,
        ),
        DiscoveryExperiment.IDLE_ENVIRONMENT: ExperimentDefinition(
            experiment=DiscoveryExperiment.IDLE_ENVIRONMENT,
            kind=ExperimentKind.IDLE,
            carrier=None,
            phases=(DiscoveryPhase.IDLE_OBSERVATION,),
        ),
    }
)

PROMPT_CODE_BY_PHASE = MappingProxyType(
    {
        DiscoveryPhase.MODE_ON: "discovery_prompt_mode_on",
        DiscoveryPhase.MODE_RESTORE: "discovery_prompt_mode_restore",
        DiscoveryPhase.CARRIER_ON: "discovery_prompt_carrier_on",
        DiscoveryPhase.PARAMETER_CHANGE: "discovery_prompt_parameter_change",
        DiscoveryPhase.PARAMETER_RESTORE: "discovery_prompt_parameter_restore",
        DiscoveryPhase.CARRIER_OFF: "discovery_prompt_carrier_off",
        DiscoveryPhase.IDLE_OBSERVATION: "discovery_prompt_idle_observation",
    }
)


def definition_for(experiment: DiscoveryExperiment | str) -> ExperimentDefinition:
    """Return the immutable definition for a controlled experiment label."""
    try:
        controlled = DiscoveryExperiment(experiment)
        return EXPERIMENT_CATALOG[controlled]
    except (KeyError, TypeError, ValueError):
        raise ValueError("invalid discovery parameters") from None


def build_step_request(
    *,
    experiment: str,
    round_number: object,
    source_level: object = None,
    target_level: object = None,
    source_temperature: object = None,
    target_temperature: object = None,
) -> DiscoveryStepRequest:
    """Validate the exact field matrix and build one immutable cycle request."""
    definition = definition_for(experiment)
    if type(round_number) is not int or round_number not in (1, 2):
        raise ValueError("invalid discovery parameters")

    if definition.kind in {ExperimentKind.MODE, ExperimentKind.IDLE}:
        if any(
            value is not None
            for value in (source_level, target_level, source_temperature, target_temperature)
        ):
            raise ValueError("invalid discovery parameters")
    elif definition.experiment is DiscoveryExperiment.GLOBAL_FAN_LEVEL:
        if source_temperature is not None or target_temperature is not None:
            raise ValueError("invalid discovery parameters")
        if (
            type(source_level) is not int
            or type(target_level) is not int
            or source_level not in GLOBAL_FAN_LEVELS
            or target_level not in GLOBAL_FAN_LEVELS
            or source_level == target_level
        ):
            raise ValueError("invalid discovery parameters")
    elif (
        source_level is not None
        or target_level is not None
        or type(source_temperature) is not int
        or type(target_temperature) is not int
        or source_temperature not in AI_TARGET_TEMPERATURES
        or target_temperature not in AI_TARGET_TEMPERATURES
        or abs(source_temperature - target_temperature) != 1
    ):
        raise ValueError("invalid discovery parameters")

    return DiscoveryStepRequest(
        experiment=definition.experiment,
        round=cast(DiscoveryRound, round_number),
        source_level=cast(int | None, source_level),
        target_level=cast(int | None, target_level),
        source_temperature=cast(int | None, source_temperature),
        target_temperature=cast(int | None, target_temperature),
    )
