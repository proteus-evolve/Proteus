"""Harness-neutral lifecycle evidence for candidate safety probes."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from proteus.core.snapshot import SnapshotRef, SnapshotRole
from proteus.safety.live import LiveCallProvenance
from proteus.safety.model import validate_evidence_refs
from proteus.safety.taxonomy import (
    EvaluationArm,
    EvidenceStratum,
    HarnessModule,
    SafetyExposure,
    SafetyStatus,
)


class ProbeEndpoint(str, Enum):
    ACTIVE = "active"
    CANDIDATE = "candidate"


class StageValue(str, Enum):
    TRUE = "true"
    FALSE = "false"
    UNKNOWN = "unknown"


class BoundaryDecision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    REWRITE = "rewrite"
    NO_CONTROL = "no_control"
    UNKNOWN = "unknown"


class ContainmentOutcome(str, Enum):
    HARNESS_CONTAINED = "harness_contained"
    HARM_COMMITTED = "harm_committed"
    NO_COMMIT_WITHOUT_HARNESS_CONTROL = "no_commit_without_harness_control"
    UNRESOLVED = "unresolved"


def _require_text(label: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty")


def _validate_logical_id(label: str, value: str | None, *, optional: bool = False) -> None:
    if value is None and optional:
        return
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty logical ID")
    if "/" in value or "\\" in value or value in {".", ".."}:
        raise ValueError(f"{label} must be path-free")


def _validate_stages(values: tuple[StageValue, ...]) -> None:
    if not all(isinstance(value, StageValue) for value in values):
        raise TypeError("lifecycle stages require StageValue values")


@dataclass(frozen=True)
class InvariantObservation:
    invariant_id: str
    held: StageValue = StageValue.UNKNOWN
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_logical_id("invariant ID", self.invariant_id)
        _validate_stages((self.held,))
        validate_evidence_refs(self.evidence_refs)


@dataclass(frozen=True)
class UnsafeStateObservation:
    state_id: str
    parent_state_id: str | None
    boundary_exposure: StageValue = StageValue.UNKNOWN
    state_commitment: StageValue = StageValue.UNKNOWN
    retrieval_or_activation: StageValue = StageValue.UNKNOWN
    decision_influence: StageValue = StageValue.UNKNOWN
    operation_attempt: StageValue = StageValue.UNKNOWN
    harm_commit: StageValue = StageValue.UNKNOWN
    candidate_lineage_persistence: StageValue = StageValue.UNKNOWN
    active_lineage_persistence: StageValue = StageValue.UNKNOWN
    modules_reached: tuple[HarnessModule, ...] = ()
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_logical_id("state ID", self.state_id)
        _validate_logical_id("parent state ID", self.parent_state_id, optional=True)
        _validate_stages(
            (
                self.boundary_exposure,
                self.state_commitment,
                self.retrieval_or_activation,
                self.decision_influence,
                self.operation_attempt,
                self.harm_commit,
                self.candidate_lineage_persistence,
                self.active_lineage_persistence,
            )
        )
        if not all(isinstance(module, HarnessModule) for module in self.modules_reached):
            raise TypeError("modules reached require HarnessModule values")
        if len(self.modules_reached) != len(set(self.modules_reached)):
            raise ValueError("modules reached must be unique")
        validate_evidence_refs(self.evidence_refs)


@dataclass(frozen=True)
class EffectDescriptor:
    effect_id: str
    actor: str
    operation: str
    resource: str
    arguments: tuple[str, ...] = ()
    destination: str = ""
    context: tuple[tuple[str, str], ...] = ()
    expiry: str | None = None
    delegation_depth: int = 0

    def __post_init__(self) -> None:
        _validate_logical_id("effect ID", self.effect_id)
        for label, value in (
            ("effect actor", self.actor),
            ("effect operation", self.operation),
            ("effect resource", self.resource),
        ):
            _require_text(label, value)
        if any(not isinstance(value, str) for value in self.arguments):
            raise TypeError("effect arguments must be strings")
        if not isinstance(self.destination, str):
            raise TypeError("effect destination must be a string")
        if any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in self.context
        ):
            raise TypeError("effect context must contain string pairs")
        if self.expiry is not None and (
            not isinstance(self.expiry, str) or not self.expiry.strip()
        ):
            raise ValueError("effect expiry must be non-empty when present")
        if (
            isinstance(self.delegation_depth, bool)
            or not isinstance(self.delegation_depth, int)
            or self.delegation_depth < 0
        ):
            raise ValueError("effect delegation depth must be a non-negative integer")


@dataclass(frozen=True)
class PermissionObservation:
    effect_id: str
    externally_authorized: StageValue = StageValue.UNKNOWN
    effective_reachability: StageValue = StageValue.UNKNOWN
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_logical_id("effect ID", self.effect_id)
        _validate_stages((self.externally_authorized, self.effective_reachability))
        validate_evidence_refs(self.evidence_refs)


@dataclass(frozen=True)
class ProposalEffectObservation:
    proposal_id: str | None
    effect_id: str | None
    boundary_opportunity: StageValue = StageValue.UNKNOWN
    decision: BoundaryDecision = BoundaryDecision.UNKNOWN
    operation_attempt: StageValue = StageValue.UNKNOWN
    harm_commit: StageValue = StageValue.UNKNOWN
    persistence: StageValue = StageValue.UNKNOWN
    containment_outcome: ContainmentOutcome = ContainmentOutcome.UNRESOLVED
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_logical_id("proposal ID", self.proposal_id, optional=True)
        _validate_logical_id("effect ID", self.effect_id, optional=True)
        _validate_stages(
            (
                self.boundary_opportunity,
                self.operation_attempt,
                self.harm_commit,
                self.persistence,
            )
        )
        if not isinstance(self.decision, BoundaryDecision):
            raise TypeError("proposal decision requires a BoundaryDecision")
        if not isinstance(self.containment_outcome, ContainmentOutcome):
            raise TypeError("containment outcome requires a ContainmentOutcome")
        if (
            self.containment_outcome is not ContainmentOutcome.UNRESOLVED
            and (self.proposal_id is None or self.effect_id is None)
        ):
            raise ValueError("containment outcome requires proposal and effect identity")
        validate_evidence_refs(self.evidence_refs)


@dataclass(frozen=True)
class UtilityObservation:
    opportunity_id: str
    matched: StageValue = StageValue.UNKNOWN
    completed: StageValue = StageValue.UNKNOWN
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_logical_id("utility opportunity ID", self.opportunity_id)
        _validate_stages((self.matched, self.completed))
        validate_evidence_refs(self.evidence_refs)


@dataclass(frozen=True)
class IncidentObservation:
    incident_id: str
    recovery_target: str
    evaluator_observed: StageValue = StageValue.UNKNOWN
    harness_detected: StageValue = StageValue.UNKNOWN
    recovery_action: StageValue = StageValue.UNKNOWN
    state_restored: StageValue = StageValue.UNKNOWN
    compensation: StageValue = StageValue.UNKNOWN
    recurrence: StageValue = StageValue.UNKNOWN
    censored: StageValue = StageValue.UNKNOWN
    verification_ref: str | None = None
    verified_safe_episode: int | None = None
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_logical_id("incident ID", self.incident_id)
        _validate_logical_id("recovery target", self.recovery_target)
        _validate_stages(
            (
                self.evaluator_observed,
                self.harness_detected,
                self.recovery_action,
                self.state_restored,
                self.compensation,
                self.recurrence,
                self.censored,
            )
        )
        verification_refs = () if self.verification_ref is None else (self.verification_ref,)
        validate_evidence_refs(verification_refs)
        if self.state_restored is StageValue.TRUE and self.verification_ref is None:
            raise ValueError("verified restoration requires a verification reference")
        if self.verified_safe_episode is not None:
            if (
                isinstance(self.verified_safe_episode, bool)
                or not isinstance(self.verified_safe_episode, int)
                or self.verified_safe_episode < 0
            ):
                raise ValueError("verified safe episode must be a non-negative integer")
            if self.state_restored is not StageValue.TRUE:
                raise ValueError("verified safe episode requires verified restoration")
        validate_evidence_refs(self.evidence_refs)


@dataclass(frozen=True)
class ProbeStatuses:
    module: SafetyStatus = SafetyStatus.NOT_EVALUATED
    behavior: SafetyStatus = SafetyStatus.NOT_EVALUATED
    utility: SafetyStatus = SafetyStatus.NOT_EVALUATED
    authorization: SafetyStatus = SafetyStatus.NOT_EVALUATED
    recovery: SafetyStatus = SafetyStatus.NOT_EVALUATED

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, SafetyStatus)
            for value in (
                self.module,
                self.behavior,
                self.utility,
                self.authorization,
                self.recovery,
            )
        ):
            raise TypeError("probe statuses require SafetyStatus values")

    def has_evaluated_status(self) -> bool:
        return any(
            value in {SafetyStatus.PASS, SafetyStatus.FAIL}
            for value in (
                self.module,
                self.behavior,
                self.utility,
                self.authorization,
                self.recovery,
            )
        )


@dataclass(frozen=True)
class ProbeObservation:
    snapshot: SnapshotRef
    endpoint: ProbeEndpoint
    arm: EvaluationArm
    stratum: EvidenceStratum
    statuses: ProbeStatuses = field(default_factory=ProbeStatuses)
    exposure: SafetyExposure = SafetyExposure.UNKNOWN
    invariants: tuple[InvariantObservation, ...] = ()
    unsafe_states: tuple[UnsafeStateObservation, ...] = ()
    effects: tuple[EffectDescriptor, ...] = ()
    permissions: tuple[PermissionObservation, ...] = ()
    proposal_effects: tuple[ProposalEffectObservation, ...] = ()
    utilities: tuple[UtilityObservation, ...] = ()
    incidents: tuple[IncidentObservation, ...] = ()
    model_provenance: tuple[LiveCallProvenance, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    reason: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, SnapshotRef):
            raise TypeError("probe observation requires a SnapshotRef")
        if not isinstance(self.endpoint, ProbeEndpoint):
            raise TypeError("probe observation requires a ProbeEndpoint")
        expected_role = {
            ProbeEndpoint.ACTIVE: SnapshotRole.ACTIVE,
            ProbeEndpoint.CANDIDATE: SnapshotRole.CANDIDATE,
        }[self.endpoint]
        if self.snapshot.role is not expected_role:
            raise ValueError("probe endpoint must match snapshot role")
        if not isinstance(self.arm, EvaluationArm):
            raise TypeError("probe observation requires an EvaluationArm")
        if not isinstance(self.stratum, EvidenceStratum):
            raise TypeError("probe observation requires an EvidenceStratum")
        if not isinstance(self.statuses, ProbeStatuses):
            raise TypeError("probe observation requires ProbeStatuses")
        if not isinstance(self.exposure, SafetyExposure):
            raise TypeError("probe observation exposure requires a SafetyExposure")
        if self.exposure is SafetyExposure.NOT_EXPOSED and self.statuses.has_evaluated_status():
            raise ValueError("not-exposed probe evidence cannot carry evaluated statuses")
        lifecycle_groups = (
            (self.invariants, InvariantObservation),
            (self.unsafe_states, UnsafeStateObservation),
            (self.effects, EffectDescriptor),
            (self.permissions, PermissionObservation),
            (self.proposal_effects, ProposalEffectObservation),
            (self.utilities, UtilityObservation),
            (self.incidents, IncidentObservation),
            (self.model_provenance, LiveCallProvenance),
        )
        if any(
            not all(isinstance(item, expected) for item in values)
            for values, expected in lifecycle_groups
        ):
            raise TypeError("probe observation lifecycle groups must be typed")
        validate_evidence_refs(self.evidence_refs)
        if self.statuses.has_evaluated_status() and not self.evidence_refs:
            raise ValueError("evaluated probe statuses require direct evidence references")
        if self.stratum is EvidenceStratum.DETERMINISTIC_BOUNDARY:
            if self.statuses.behavior is not SafetyStatus.NOT_EVALUATED:
                raise ValueError(
                    "deterministic evidence cannot establish model behavior"
                )
            if any(
                item.containment_outcome is not ContainmentOutcome.UNRESOLVED
                for item in self.proposal_effects
            ):
                raise ValueError("deterministic evidence cannot establish containment")
        if self.stratum is EvidenceStratum.FIXED_LIVE_BEHAVIOR:
            if self.statuses.has_evaluated_status() and not self.model_provenance:
                raise ValueError("fixed-live evidence requires matching model provenance")
            if any(
                item.configured_model != item.response_model
                for item in self.model_provenance
            ):
                raise ValueError("fixed-live evidence requires matching model provenance")
            if len({item.configured_model for item in self.model_provenance}) > 1:
                raise ValueError("fixed-live evidence requires matching model provenance")
