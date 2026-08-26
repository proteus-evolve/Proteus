"""Harness-neutral declarations for activation-time safety families."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class HarnessModule(str, Enum):
    AGENT_LOOP = "agent_loop"
    MEMORY = "memory"
    SKILLS = "skills"
    TOOLS = "tools"


class CaseScope(str, Enum):
    SINGLE_MODULE = "single_module"
    CROSS_MODULE = "cross_module"


class SafetyKind(str, Enum):
    NON_ADVERSARIAL = "non_adversarial"
    ADVERSARIAL = "adversarial"


class EvaluationArm(str, Enum):
    MODEL_REFERENCE = "model_reference"
    FULL_HARNESS = "full_harness"
    MODULE_INTERVENTION = "module_intervention"


class EvidenceStratum(str, Enum):
    NATIVE_BOUNDARY = "native_boundary"
    REAL_EPISODE = "real_episode"
    ARCHIVE_LINEAGE = "archive_lineage"


class SafetyStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    NOT_EVALUATED = "not_evaluated"
    INVALID = "invalid"
    ERROR = "error"


class SafetyExposure(str, Enum):
    EXPOSED = "exposed"
    NOT_EXPOSED = "not_exposed"
    UNKNOWN = "unknown"


class SafetyIndicator(str, Enum):
    INVARIANT_PRESERVATION = "invariant_preservation"
    UNSAFE_STATE_PROPAGATION = "unsafe_state_propagation"
    PERMISSION_BOUNDARY_INTEGRITY = "permission_boundary_integrity"
    HARM_CONTAINMENT = "harm_containment"
    RECOVERY_ROLLBACK = "recovery_rollback"


def _require_text(label: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty")


@dataclass(frozen=True)
class FaultModel:
    source: str
    condition: str

    def __post_init__(self) -> None:
        _require_text("fault source", self.source)
        _require_text("fault condition", self.condition)


@dataclass(frozen=True)
class ThreatModel:
    attacker: str
    defender: str
    entry_point: str
    attacker_capabilities: tuple[str, ...]
    attacker_capability_limits: tuple[str, ...]
    attacker_objective: str
    protected_invariant: str
    defensive_control_under_test: str

    def __post_init__(self) -> None:
        for label, value in (
            ("attacker", self.attacker),
            ("defender", self.defender),
            ("entry point", self.entry_point),
            ("attacker objective", self.attacker_objective),
            ("protected invariant", self.protected_invariant),
            ("defensive control", self.defensive_control_under_test),
        ):
            _require_text(label, value)
        if not self.attacker_capabilities or not self.attacker_capability_limits:
            raise ValueError("threat model capabilities and limits must be non-empty")


@dataclass(frozen=True)
class PermissionBoundary:
    actor: str
    requested_operation: str
    allowed_capabilities: tuple[str, ...]
    prohibited_capabilities: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_text("permission actor", self.actor)
        _require_text("requested operation", self.requested_operation)
        if not self.allowed_capabilities or not self.prohibited_capabilities:
            raise ValueError("permission capabilities must be non-empty")


@dataclass(frozen=True)
class SafetyInvariantDefinition:
    invariant_id: str
    statement: str

    def __post_init__(self) -> None:
        _require_text("invariant ID", self.invariant_id)
        _require_text("invariant statement", self.statement)


@dataclass(frozen=True)
class IndicatorRequirement:
    indicator: SafetyIndicator
    critical: bool
    required_strata: tuple[EvidenceStratum, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.indicator, SafetyIndicator):
            raise TypeError("indicator requirement needs a SafetyIndicator")
        if not self.required_strata:
            raise ValueError("indicator requirement needs evidence strata")
        if len(self.required_strata) != len(set(self.required_strata)):
            raise ValueError("indicator requirement strata must be unique")


@dataclass(frozen=True)
class EvaluationCell:
    cell_id: str
    arm: EvaluationArm
    stratum: EvidenceStratum

    def __post_init__(self) -> None:
        _require_text("evaluation cell ID", self.cell_id)
        if not isinstance(self.arm, EvaluationArm):
            raise TypeError("evaluation cell requires an EvaluationArm")
        if not isinstance(self.stratum, EvidenceStratum):
            raise TypeError("evaluation cell requires an EvidenceStratum")


@dataclass(frozen=True)
class SafetyCaseFamilyDefinition:
    family_id: str
    family_version: str
    primary_module: HarnessModule
    supporting_modules: tuple[HarnessModule, ...]
    scope: CaseScope
    safety_kind: SafetyKind
    scenario: str
    invariant: SafetyInvariantDefinition
    indicator_requirements: tuple[IndicatorRequirement, ...]
    utility_minimum: float
    exposure_rule: str
    behavior_failure: str
    module_failure: str
    evaluation_arms: tuple[EvaluationArm, ...]
    declared_cells: tuple[EvaluationCell, ...]
    threat_model: ThreatModel | None = None
    fault_model: FaultModel | None = None
    permission_boundary: PermissionBoundary | None = None

    def __post_init__(self) -> None:
        for label, value in (
            ("family ID", self.family_id),
            ("family version", self.family_version),
            ("scenario", self.scenario),
            ("exposure rule", self.exposure_rule),
            ("behavior failure", self.behavior_failure),
            ("module failure", self.module_failure),
        ):
            _require_text(label, value)
        if self.primary_module in self.supporting_modules:
            raise ValueError("primary module cannot also support its family")
        if self.scope is CaseScope.SINGLE_MODULE and self.supporting_modules:
            raise ValueError("single-module family cannot have supporting modules")
        if self.scope is CaseScope.CROSS_MODULE and not self.supporting_modules:
            raise ValueError("cross-module family requires supporting modules")
        if not self.indicator_requirements or not self.declared_cells:
            raise ValueError("family requires indicators and declared cells")
        if len({cell.cell_id for cell in self.declared_cells}) != len(self.declared_cells):
            raise ValueError("family evaluation cell IDs must be unique")
        if {cell.arm for cell in self.declared_cells} != set(self.evaluation_arms):
            raise ValueError("declared cells and evaluation arms must agree")
        if not 0.0 <= self.utility_minimum <= 1.0:
            raise ValueError("utility minimum must be between zero and one")
        if self.safety_kind is SafetyKind.ADVERSARIAL:
            if self.threat_model is None or self.fault_model is not None:
                raise ValueError("adversarial family requires only a threat model")
        elif (
            self.safety_kind is SafetyKind.NON_ADVERSARIAL
            and (self.fault_model is None or self.threat_model is not None)
        ):
            raise ValueError("non-adversarial family requires only a fault model")
