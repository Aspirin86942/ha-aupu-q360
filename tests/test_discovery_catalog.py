"""Behavior tests for the fixed Q360 panel discovery experiment catalog."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from custom_components.aupu_q360 import discovery_models
from custom_components.aupu_q360.discovery_catalog import (
    AI_TARGET_TEMPERATURES,
    EXPERIMENT_CATALOG,
    GLOBAL_FAN_LEVELS,
    MODE_EXPERIMENTS,
    PROMPT_CODE_BY_PHASE,
    ExperimentDefinition,
    build_step_request,
    definition_for,
)
from custom_components.aupu_q360.discovery_models import (
    DiscoveryCoverage,
    DiscoveryExperiment,
    DiscoveryPhase,
    DiscoveryProgress,
    DiscoveryState,
    ExperimentKind,
)

_MODE_NAMES = (
    "ai_thermostatic_warmth",
    "deodorization_sterilization",
    "ventilation",
    "air_blowing",
    "normal_drying",
    "thermostatic_drying",
    "night_light",
)


def test_v1_model_surface_is_not_exported() -> None:
    """Catch removed capability/target APIs surviving beside the v2 catalog."""
    for name in (
        "DiscoveryCapability",
        "DiscoveryTarget",
        "StepLabel",
        "StepEvidence",
    ):
        assert not hasattr(discovery_models, name)


def test_catalog_has_the_fixed_order_ranges_and_carriers() -> None:
    """Catch experiments, legal values, or parameter carriers drifting from the spec."""
    assert tuple(EXPERIMENT_CATALOG) == (
        DiscoveryExperiment.AI_THERMOSTATIC_WARMTH,
        DiscoveryExperiment.DEODORIZATION_STERILIZATION,
        DiscoveryExperiment.VENTILATION,
        DiscoveryExperiment.AIR_BLOWING,
        DiscoveryExperiment.NORMAL_DRYING,
        DiscoveryExperiment.THERMOSTATIC_DRYING,
        DiscoveryExperiment.NIGHT_LIGHT,
        DiscoveryExperiment.GLOBAL_FAN_LEVEL,
        DiscoveryExperiment.AI_TARGET_TEMPERATURE,
        DiscoveryExperiment.IDLE_ENVIRONMENT,
    )
    assert MODE_EXPERIMENTS == tuple(DiscoveryExperiment(value) for value in _MODE_NAMES)
    assert GLOBAL_FAN_LEVELS == (1, 2, 3, 4, 5)
    assert AI_TARGET_TEMPERATURES == tuple(range(30, 43))
    assert definition_for(DiscoveryExperiment.GLOBAL_FAN_LEVEL).carrier is (
        DiscoveryExperiment.VENTILATION
    )
    assert definition_for(DiscoveryExperiment.AI_TARGET_TEMPERATURE).carrier is (
        DiscoveryExperiment.AI_THERMOSTATIC_WARMTH
    )


def test_definitions_use_the_exact_phase_sequences() -> None:
    """Catch a workflow phase being skipped, reordered, or attributed to the wrong kind."""
    assert definition_for("night_light") == ExperimentDefinition(
        experiment=DiscoveryExperiment.NIGHT_LIGHT,
        kind=ExperimentKind.MODE,
        carrier=None,
        phases=(DiscoveryPhase.MODE_ON, DiscoveryPhase.MODE_RESTORE),
    )
    assert definition_for("global_fan_level") == ExperimentDefinition(
        experiment=DiscoveryExperiment.GLOBAL_FAN_LEVEL,
        kind=ExperimentKind.PARAMETER,
        carrier=DiscoveryExperiment.VENTILATION,
        phases=(
            DiscoveryPhase.CARRIER_ON,
            DiscoveryPhase.PARAMETER_CHANGE,
            DiscoveryPhase.PARAMETER_RESTORE,
            DiscoveryPhase.CARRIER_OFF,
        ),
    )
    assert definition_for("idle_environment") == ExperimentDefinition(
        experiment=DiscoveryExperiment.IDLE_ENVIRONMENT,
        kind=ExperimentKind.IDLE,
        carrier=None,
        phases=(DiscoveryPhase.IDLE_OBSERVATION,),
    )


def test_catalog_and_definitions_are_immutable() -> None:
    """Catch runtime code mutating the fixed experiment contract for later requests."""
    with pytest.raises(TypeError):
        EXPERIMENT_CATALOG[DiscoveryExperiment.NIGHT_LIGHT] = definition_for(  # type: ignore[index]
            DiscoveryExperiment.NIGHT_LIGHT
        )
    with pytest.raises(FrozenInstanceError):
        definition_for("night_light").carrier = DiscoveryExperiment.VENTILATION  # type: ignore[misc]


@pytest.mark.parametrize("experiment", (*_MODE_NAMES, "idle_environment"))
@pytest.mark.parametrize("round_number", (1, 2))
def test_mode_and_idle_requests_accept_only_their_controlled_labels(
    experiment: str,
    round_number: int,
) -> None:
    """Catch a valid mode or idle cycle being rejected or assigned an unstable ID."""
    request = build_step_request(experiment=experiment, round_number=round_number)

    assert request.experiment is DiscoveryExperiment(experiment)
    assert request.round == round_number
    assert request.cycle_id == f"{experiment}:{round_number}"
    assert request.source_level is None
    assert request.target_level is None
    assert request.source_temperature is None
    assert request.target_temperature is None


@pytest.mark.parametrize(
    ("source", "target", "round_number", "cycle_id"),
    (
        (1, 5, 1, "global_fan_level:1:5:1"),
        (3, 5, 2, "global_fan_level:3:5:2"),
        (5, 2, 1, "global_fan_level:5:2:1"),
    ),
)
def test_fan_requests_accept_distinct_fixed_levels(
    source: int,
    target: int,
    round_number: int,
    cycle_id: str,
) -> None:
    """Catch a legal five-level comparison losing either endpoint or round identity."""
    request = build_step_request(
        experiment="global_fan_level",
        round_number=round_number,
        source_level=source,
        target_level=target,
    )

    assert request.cycle_id == cycle_id
    assert (request.source_level, request.target_level) == (source, target)


@pytest.mark.parametrize(
    ("source", "target", "round_number", "cycle_id"),
    (
        (30, 31, 1, "ai_target_temperature:30:31:1"),
        (35, 36, 2, "ai_target_temperature:35:36:2"),
        (42, 41, 1, "ai_target_temperature:42:41:1"),
    ),
)
def test_temperature_requests_accept_adjacent_fixed_values(
    source: int,
    target: int,
    round_number: int,
    cycle_id: str,
) -> None:
    """Catch a legal adjacent-temperature comparison losing its direction or round."""
    request = build_step_request(
        experiment="ai_target_temperature",
        round_number=round_number,
        source_temperature=source,
        target_temperature=target,
    )

    assert request.cycle_id == cycle_id
    assert (request.source_temperature, request.target_temperature) == (source, target)


@pytest.mark.parametrize("round_number", (0, 3, True, False, 1.0, "1", None))
def test_requests_reject_nonliteral_rounds(round_number: object) -> None:
    """Catch booleans or lookalike values entering a two-round experiment."""
    with pytest.raises(ValueError, match="invalid discovery parameters"):
        build_step_request(experiment="night_light", round_number=round_number)


@pytest.mark.parametrize(
    "field",
    ("source_level", "target_level", "source_temperature", "target_temperature"),
)
@pytest.mark.parametrize("experiment", ("night_light", "idle_environment"))
def test_mode_and_idle_requests_reject_all_parameter_fields(
    experiment: str,
    field: str,
) -> None:
    """Catch caller-supplied parameter data contaminating mode or idle cycles."""
    with pytest.raises(ValueError, match="invalid discovery parameters"):
        build_step_request(experiment=experiment, round_number=1, **{field: 1})


@pytest.mark.parametrize(
    ("source", "target"),
    (
        (None, 2),
        (2, None),
        (2, 2),
        (0, 2),
        (2, 6),
        (True, 2),
        (2, False),
        (1.0, 2),
        (2, 3.0),
        ("1", 2),
    ),
)
def test_fan_requests_reject_missing_equal_out_of_range_or_noninteger_levels(
    source: object,
    target: object,
) -> None:
    """Catch invalid fan-level endpoints entering an experiment request."""
    with pytest.raises(ValueError, match="invalid discovery parameters"):
        build_step_request(
            experiment="global_fan_level",
            round_number=1,
            source_level=source,
            target_level=target,
        )


@pytest.mark.parametrize(
    ("source", "target"),
    (
        (None, 31),
        (30, None),
        (30, 30),
        (30, 32),
        (29, 30),
        (42, 43),
        (True, 31),
        (30, False),
        (30.0, 31),
        (30, 31.0),
        ("30", 31),
    ),
)
def test_temperature_requests_reject_invalid_endpoints(source: object, target: object) -> None:
    """Catch non-adjacent, out-of-range, or noninteger temperature values."""
    with pytest.raises(ValueError, match="invalid discovery parameters"):
        build_step_request(
            experiment="ai_target_temperature",
            round_number=1,
            source_temperature=source,
            target_temperature=target,
        )


def test_parameter_requests_reject_fields_for_the_other_parameter_kind() -> None:
    """Catch fan and temperature endpoints crossing experiment boundaries."""
    with pytest.raises(ValueError, match="invalid discovery parameters"):
        build_step_request(
            experiment="global_fan_level",
            round_number=1,
            source_level=1,
            target_level=2,
            source_temperature=30,
            target_temperature=31,
        )
    with pytest.raises(ValueError, match="invalid discovery parameters"):
        build_step_request(
            experiment="ai_target_temperature",
            round_number=1,
            source_temperature=30,
            target_temperature=31,
            source_level=1,
            target_level=2,
        )


def test_unknown_experiments_are_rejected_with_a_fixed_error() -> None:
    """Catch arbitrary labels entering stable cycle identifiers or reports."""
    with pytest.raises(ValueError, match="invalid discovery parameters"):
        build_step_request(experiment="private-mode-name", round_number=1)
    with pytest.raises(ValueError, match="invalid discovery parameters"):
        definition_for("private-mode-name")


def test_progress_response_contains_only_the_fixed_public_fields() -> None:
    """Catch internal request, raw evidence, or free text leaking through progress responses."""
    progress = DiscoveryProgress(
        state=DiscoveryState.AWAITING_OPERATOR,
        message_code="discovery_prompt_mode_restore",
        phase=DiscoveryPhase.MODE_RESTORE,
        completed_cycle_count=3,
        manual_restore_required=False,
    )

    assert progress.to_response() == {
        "state": "awaiting_operator",
        "message_code": "discovery_prompt_mode_restore",
        "phase": "mode_restore",
        "completed_cycle_count": 3,
        "manual_restore_required": False,
    }
    assert DiscoveryProgress(
        state=DiscoveryState.READY,
        message_code="discovery_ready_for_step",
    ).to_response() == {
        "state": "ready",
        "message_code": "discovery_ready_for_step",
        "completed_cycle_count": 0,
        "manual_restore_required": False,
    }


def test_controlled_enum_and_prompt_values_match_the_v2_contract() -> None:
    """Catch report coverage or operator prompt values drifting from the fixed API."""
    assert tuple(item.value for item in DiscoveryState) == (
        "idle",
        "archive_opening",
        "session_baselining",
        "ready",
        "step_baselining",
        "awaiting_operator",
        "restore_required",
        "finalizing",
        "cancelled",
    )
    assert tuple(item.value for item in DiscoveryCoverage) == (
        "not_started",
        "partial",
        "complete",
    )
    assert PROMPT_CODE_BY_PHASE == {
        DiscoveryPhase.MODE_ON: "discovery_prompt_mode_on",
        DiscoveryPhase.MODE_RESTORE: "discovery_prompt_mode_restore",
        DiscoveryPhase.CARRIER_ON: "discovery_prompt_carrier_on",
        DiscoveryPhase.PARAMETER_CHANGE: "discovery_prompt_parameter_change",
        DiscoveryPhase.PARAMETER_RESTORE: "discovery_prompt_parameter_restore",
        DiscoveryPhase.CARRIER_OFF: "discovery_prompt_carrier_off",
        DiscoveryPhase.IDLE_OBSERVATION: "discovery_prompt_idle_observation",
    }
