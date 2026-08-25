"""Plug-in contracts for future module-first harness-safety case suites."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Protocol, runtime_checkable

from proteus.core.adapter import ActionEvent
from proteus.core.snapshot import SnapshotRef
from proteus.safety.evidence import ProbeEndpoint, ProbeObservation
from proteus.safety.live import LiveModelChannel
from proteus.safety.model import validate_evidence_refs
from proteus.safety.taxonomy import (
    EvaluationArm,
    EvidenceStratum,
    HarnessSafetyProfile,
    SafetyCaseFamilyDefinition,
    SafetyExposure,
)


class ModelBehavior(str, Enum):
    REFUSED = "refused"
    SAFE_RESPONSE = "safe_response"
    UNSAFE_RESPONSE = "unsafe_response"
    UNSAFE_ACTION_PROPOSAL = "unsafe_action_proposal"
    NOT_EVALUATED = "not_evaluated"


class HarnessDecision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    REWRITE = "rewrite"
    NO_CONTROL = "no_control"
    NOT_APPLICABLE = "not_applicable"
    NOT_EVALUATED = "not_evaluated"


@dataclass(frozen=True)
class ResponsibilityObservation:
    model_behavior: ModelBehavior = ModelBehavior.NOT_EVALUATED
    harness_decision: HarnessDecision = HarnessDecision.NOT_EVALUATED
    behavior_invariant_violated: bool | None = None
    operation_attempted: bool | None = None
    harmful_effect_committed: bool | None = None
    persisted_or_propagated: bool | None = None
    contained: bool | None = None
    recovered: bool | None = None
    utility_preserved: bool | None = None


@dataclass(frozen=True)
class ModuleObservation:
    invariant_violated: bool | None = None
    module_input_observed: bool | None = None
    module_output_observed: bool | None = None


@dataclass(frozen=True)
class HarnessSafetyEvidence:
    arm: EvaluationArm
    evaluable: bool
    exposure: SafetyExposure = SafetyExposure.UNKNOWN
    responsibility: ResponsibilityObservation = field(
        default_factory=ResponsibilityObservation
    )
    module: ModuleObservation = field(default_factory=ModuleObservation)
    observed_behavior: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    reason: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.arm, EvaluationArm):
            raise TypeError("harness-safety evidence arm requires an EvaluationArm")
        validate_evidence_refs(self.evidence_refs)
        if self.evaluable and not self.evidence_refs:
            raise ValueError("evaluable harness-safety evidence requires evidence references")
        if not self.evaluable and not self.reason.strip():
            raise ValueError("unevaluable harness-safety evidence requires a reason")
        if self.exposure is SafetyExposure.NOT_EXPOSED and self.evaluable:
            raise ValueError("not-exposed evidence cannot be evaluable")
        if (
            self.arm is EvaluationArm.MODEL_REFERENCE
            and self.responsibility.harmful_effect_committed is True
        ):
            raise ValueError("dry-run model reference cannot commit a harmful effect")
        if (
            self.arm is EvaluationArm.MODEL_REFERENCE
            and self.responsibility.harness_decision
            not in {HarnessDecision.NOT_APPLICABLE, HarnessDecision.NOT_EVALUATED}
        ):
            raise ValueError("model reference cannot carry a harness decision")


@dataclass(frozen=True)
class HarnessSafetyContext:
    run_id: str
    adapter_name: str
    arm: str
    seed: int
    episode: int
    snapshot_root: Path
    evaluation_root: Path
    evidence_dir: Path
    profile: HarnessSafetyProfile
    events: tuple[ActionEvent, ...] = ()


@runtime_checkable
class HarnessSafetyCaseSuite(Protocol):
    name: str
    version: str

    def definitions(self) -> Sequence[SafetyCaseFamilyDefinition]: ...


@runtime_checkable
class HarnessSafetyAdapter(Protocol):
    def harness_safety_profile(self) -> HarnessSafetyProfile: ...


@dataclass(frozen=True)
class CandidateSafetyContext:
    run_id: str
    episode: int
    adapter_name: str
    snapshot: SnapshotRef
    snapshot_root: Path
    trial_root: Path
    evidence_dir: Path
    profile: HarnessSafetyProfile
    events: tuple[ActionEvent, ...] = ()
    controller_root: Path | None = None


@runtime_checkable
class CandidateSafetyExecutor(Protocol):
    """Adapter executor whose ``collect`` returns only after all native activity is terminal."""

    name: str

    def collect(
        self,
        definition: SafetyCaseFamilyDefinition,
        endpoint: ProbeEndpoint,
        arm: EvaluationArm,
        stratum: EvidenceStratum,
        context: CandidateSafetyContext,
        channel: LiveModelChannel | None,
    ) -> ProbeObservation: ...


@runtime_checkable
class CandidateSafetyAdapter(Protocol):
    def harness_safety_profile(self) -> HarnessSafetyProfile: ...

    def candidate_safety_executor(self) -> CandidateSafetyExecutor: ...
