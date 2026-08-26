"""Normalized native evidence for tools permission drift comparisons."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from proteus.core.snapshot import SnapshotRef
from proteus.safety.evidence import validate_evidence_refs
from proteus.safety.permission_cases import (
    PermissionArgument,
    PermissionPolicyCaseSpec,
    PermissionSemanticOperation,
)
from proteus.safety.taxonomy import SafetyStatus


class PermissionCapabilityState(str, Enum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class PermissionCaseCapability:
    state: PermissionCapabilityState
    native_mechanism: str
    missing_requirement: str

    def __post_init__(self) -> None:
        if self.state is PermissionCapabilityState.SUPPORTED:
            if not self.native_mechanism.strip():
                raise ValueError("supported capability requires a native mechanism")
            if self.missing_requirement:
                raise ValueError("supported capability cannot have a missing requirement")
        elif self.state is PermissionCapabilityState.UNSUPPORTED:
            if self.native_mechanism:
                raise ValueError("unsupported capability cannot have a native mechanism")
            if not self.missing_requirement.strip():
                raise ValueError("unsupported capability requires a missing requirement")


class NativePermissionDecisionValue(str, Enum):
    ALLOW = "allow"
    DENY = "deny"


@dataclass(frozen=True)
class NativeOperationBinding:
    operation_id: str
    semantic_operation: PermissionSemanticOperation
    logical_resource: str
    native_tool: str
    exact_arguments: tuple[PermissionArgument, ...]
    raw_event_root: str

    def __post_init__(self) -> None:
        validate_evidence_refs((self.raw_event_root,))


@dataclass(frozen=True)
class NativePermissionBinding:
    case_id: str
    native_mechanism: str
    protected: NativeOperationBinding
    allowed_control: NativeOperationBinding


@dataclass(frozen=True)
class NativeProposal:
    correlation_id: str
    native_tool: str
    exact_arguments: tuple[PermissionArgument, ...]
    raw_event_ref: str

    def __post_init__(self) -> None:
        validate_evidence_refs((self.raw_event_ref,))


@dataclass(frozen=True)
class NativeDecision:
    correlation_id: str
    value: NativePermissionDecisionValue
    source: str
    rule_ref: str
    reason: str
    raw_event_ref: str

    def __post_init__(self) -> None:
        validate_evidence_refs((self.raw_event_ref,))


@dataclass(frozen=True)
class NativeAttemptResult:
    correlation_id: str
    attempted: bool
    completed: bool
    native_success: bool
    native_error: str
    raw_event_ref: str

    def __post_init__(self) -> None:
        validate_evidence_refs((self.raw_event_ref,))


@dataclass(frozen=True)
class NativeDelivery:
    correlation_id: str
    delivered: bool
    later_turn_id: str
    raw_input_ref: str

    def __post_init__(self) -> None:
        validate_evidence_refs((self.raw_input_ref,))


@dataclass(frozen=True)
class CanaryObservation:
    operation_id: str
    observed: bool
    effect_committed: bool
    raw_oracle_ref: str

    def __post_init__(self) -> None:
        validate_evidence_refs((self.raw_oracle_ref,))


@dataclass(frozen=True)
class NativePermissionTrace:
    snapshot: SnapshotRef
    case_id: str
    operation_id: str
    proposal: NativeProposal | None
    decision: NativeDecision | None
    attempt_result: NativeAttemptResult | None
    delivery: NativeDelivery | None
    canary: CanaryObservation | None


class PermissionComparisonStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    BASELINE_FAILURE = "baseline_failure"
    NOT_EVALUATED = "not_evaluated"


class PermissionEvidenceValidity(str, Enum):
    VALID = "valid"
    INVALID = "invalid"
    ERROR = "error"


def _require_current_versions(family_id: str, family_version: str, schema_version: str) -> None:
    if family_id != "tools_permission_drift":
        raise ValueError("permission comparison requires tools_permission_drift")
    if family_version != "2" or schema_version != "2":
        raise ValueError("permission comparison requires family and schema version 2")


@dataclass(frozen=True)
class PermissionCaseComparison:
    family_id: str
    family_version: str
    schema_version: str
    active_snapshot: SnapshotRef
    candidate_snapshot: SnapshotRef
    case_id: str
    case_spec: PermissionPolicyCaseSpec
    active_capability: PermissionCaseCapability
    candidate_capability: PermissionCaseCapability
    active_protected: NativePermissionTrace | None
    active_allowed: NativePermissionTrace | None
    candidate_protected: NativePermissionTrace | None
    candidate_allowed: NativePermissionTrace | None
    validity: PermissionEvidenceValidity
    comparison_status: PermissionComparisonStatus
    reasons: tuple[str, ...]
    evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_current_versions(self.family_id, self.family_version, self.schema_version)
        validate_evidence_refs(self.evidence_refs)


@dataclass(frozen=True)
class PermissionFamilyComparison:
    family_id: str
    family_version: str
    schema_version: str
    active_snapshot: SnapshotRef
    candidate_snapshot: SnapshotRef
    cases: tuple[PermissionCaseComparison, ...]
    comparison_status: PermissionComparisonStatus
    validity: PermissionEvidenceValidity
    terminal_status: SafetyStatus
    blockers: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_current_versions(self.family_id, self.family_version, self.schema_version)
