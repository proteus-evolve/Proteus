"""Trajectory-shaped safety indicators for settled post-episode evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal

from proteus.safety.permission_evidence import PermissionEvidenceValidity
from proteus.safety.taxonomy import SafetyStatus

ScheduleStatus = Literal["evaluated", "not_scheduled", "not_evaluated", "error"]

STABLE_SAFE = "stable_safe"
REGRESSION = "regression"
REPAIR = "repair"
PERSISTENT_SAFETY_DEBT = "persistent_safety_debt"
STABLE_UTILITY = "stable_utility"
UTILITY_REGRESSION = "utility_regression"
UTILITY_REPAIR = "utility_repair"
PERSISTENT_UTILITY_DEBT = "persistent_utility_debt"
UNAVAILABLE = "unavailable"


class SafetyChange(str, Enum):
    """A status transition derived from two stored observations."""

    STABLE_SAFE = "stable_safe"
    REGRESSION = "regression"
    REPAIR = "repair"
    PERSISTENT_UNSAFE = "persistent_unsafe"
    UNAVAILABLE = "unavailable"


def classify_status_change(
    previous: SafetyStatus | None,
    current: SafetyStatus | None,
) -> SafetyChange:
    """Classify a pass/fail transition without evaluating either observation."""
    if previous not in {SafetyStatus.PASS, SafetyStatus.FAIL}:
        return SafetyChange.UNAVAILABLE
    if current not in {SafetyStatus.PASS, SafetyStatus.FAIL}:
        return SafetyChange.UNAVAILABLE
    if previous is SafetyStatus.PASS and current is SafetyStatus.PASS:
        return SafetyChange.STABLE_SAFE
    if previous is SafetyStatus.PASS and current is SafetyStatus.FAIL:
        return SafetyChange.REGRESSION
    if previous is SafetyStatus.FAIL and current is SafetyStatus.PASS:
        return SafetyChange.REPAIR
    return SafetyChange.PERSISTENT_UNSAFE


def binary_safety_transition(previous: str | None, current: str | None) -> str | None:
    """Classify a pass/fail pair. Missing observations are unavailable."""
    if previous is None or current is None:
        return None
    if previous not in {SafetyStatus.PASS.value, SafetyStatus.FAIL.value}:
        return UNAVAILABLE if current else None
    if current not in {SafetyStatus.PASS.value, SafetyStatus.FAIL.value}:
        return UNAVAILABLE
    if previous == SafetyStatus.PASS.value and current == SafetyStatus.PASS.value:
        return STABLE_SAFE
    if previous == SafetyStatus.PASS.value and current == SafetyStatus.FAIL.value:
        return REGRESSION
    if previous == SafetyStatus.FAIL.value and current == SafetyStatus.PASS.value:
        return REPAIR
    return PERSISTENT_SAFETY_DEBT


def binary_utility_transition(previous: bool | None, current: bool | None) -> str | None:
    """Allowed-twin axis: committed is the healthy utility outcome."""
    if previous is None or current is None:
        return None
    if previous and current:
        return STABLE_UTILITY
    if previous and not current:
        return UTILITY_REGRESSION
    if not previous and current:
        return UTILITY_REPAIR
    return PERSISTENT_UTILITY_DEBT


def protected_safety_transition(
    previous_committed: bool | None, current_committed: bool | None
) -> str | None:
    """Protected axis: blocked (not committed) is the healthy safety outcome."""
    if previous_committed is None or current_committed is None:
        return None
    previous = SafetyStatus.FAIL.value if previous_committed else SafetyStatus.PASS.value
    current = SafetyStatus.FAIL.value if current_committed else SafetyStatus.PASS.value
    return binary_safety_transition(previous, current)


@dataclass(frozen=True)
class FamilyExecution:
    schedule_status: ScheduleStatus
    last_observed_episode: int | None
    evidence_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class FamilyEvolutionDelta:
    previous_episode: int | None
    current_episode: int
    step_change: str | None
    baseline_change: str | None


@dataclass(frozen=True)
class MemoryBadAdmissionIndicators:
    execution: FamilyExecution
    retrieve_status: SafetyStatus
    follow_status: SafetyStatus
    original_utility_status: SafetyStatus
    unsafe_retrievable: bool | None
    unsafe_followed: bool | None
    original_still_present: bool | None
    retrieve_delta: FamilyEvolutionDelta | None = None
    follow_delta: FamilyEvolutionDelta | None = None
    original_utility_delta: FamilyEvolutionDelta | None = None
    eligible_state_ids: tuple[str, ...] = ()
    matched_persistent_records: int = 0
    exact_overwrite_evaluated: int = 0
    exact_overwrite_failures: int = 0
    near_shadow_evaluated: int = 0
    trusted_top_1_count: int = 0
    trusted_top_3_count: int = 0
    advbench_exposure_at_1_count: int = 0
    advbench_exposure_at_3_count: int = 0
    provenance_failure_count: int = 0
    benign_controls_attempted: int = 0
    benign_controls_succeeded: int = 0
    behavior_trials: int = 0
    advbench_uptake_trials: int = 0


@dataclass(frozen=True)
class MemoryCollapseIndicators:
    execution: FamilyExecution
    filler_admitted: SafetyStatus
    prior_memory_preserved: SafetyStatus
    prior_memory_retrievable: SafetyStatus
    items_before: int
    items_preserved: int
    items_lost: int
    items_revised: int
    filler_delta: FamilyEvolutionDelta | None = None
    preserved_delta: FamilyEvolutionDelta | None = None
    retrievable_delta: FamilyEvolutionDelta | None = None


@dataclass(frozen=True)
class PermissionChange:
    protected: str | None
    allowed: str | None


@dataclass(frozen=True)
class PermissionCaseState:
    case_id: str
    protected_effect_committed: bool | None
    allowed_effect_committed: bool | None
    evidence_validity: PermissionEvidenceValidity


@dataclass(frozen=True)
class PermissionCaseIndicators:
    current: PermissionCaseState
    change_from_previous: PermissionChange | None
    change_from_baseline: PermissionChange | None


@dataclass(frozen=True)
class ToolsPermissionIndicators:
    execution: FamilyExecution
    cases: tuple[PermissionCaseIndicators, ...]


@dataclass(frozen=True)
class EpisodeSafetyIndicators:
    episode: int
    snapshot_ref: str
    memory_bad_admission: MemoryBadAdmissionIndicators
    memory_collapse: MemoryCollapseIndicators
    tools_permission_drift: ToolsPermissionIndicators
    safety_calls: int
    wall_time_s: float


@dataclass(frozen=True)
class EvolutionSafetyIndicators:
    run_id: str
    episodes: tuple[EpisodeSafetyIndicators, ...]


@dataclass(frozen=True)
class EpisodeSafetyRecord:
    """Published result of one settled-episode safety stage. Audit-only."""

    episode: int
    status: str
    decision_ref: str
    indicators: EpisodeSafetyIndicators


def unevaluated_status() -> SafetyStatus:
    return SafetyStatus.NOT_EVALUATED


def not_scheduled_execution(last_observed_episode: int | None) -> FamilyExecution:
    return FamilyExecution(
        schedule_status="not_scheduled",
        last_observed_episode=last_observed_episode,
        evidence_refs=(),
    )


def error_execution(last_observed_episode: int | None = None) -> FamilyExecution:
    return FamilyExecution(
        schedule_status="error",
        last_observed_episode=last_observed_episode,
        evidence_refs=(),
    )


def empty_admission(execution: FamilyExecution) -> MemoryBadAdmissionIndicators:
    unknown = unevaluated_status()
    return MemoryBadAdmissionIndicators(
        execution=execution,
        retrieve_status=unknown,
        follow_status=unknown,
        original_utility_status=unknown,
        unsafe_retrievable=None,
        unsafe_followed=None,
        original_still_present=None,
    )


def empty_collapse(execution: FamilyExecution) -> MemoryCollapseIndicators:
    unknown = unevaluated_status()
    return MemoryCollapseIndicators(
        execution=execution,
        filler_admitted=unknown,
        prior_memory_preserved=unknown,
        prior_memory_retrievable=unknown,
        items_before=0,
        items_preserved=0,
        items_lost=0,
        items_revised=0,
    )


def empty_permission(
    execution: FamilyExecution,
    cases: tuple[PermissionCaseIndicators, ...] = (),
) -> ToolsPermissionIndicators:
    return ToolsPermissionIndicators(execution=execution, cases=cases)
