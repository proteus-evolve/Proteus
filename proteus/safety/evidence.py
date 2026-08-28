"""Typed lifecycle evidence emitted by core-owned safety scenarios."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from proteus.core.snapshot import SnapshotRef, SnapshotRole
from proteus.safety.live import LiveCallProvenance
from proteus.safety.runtime import LogicalTransitionRecord, NativeReceipt, RuntimeKind
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
    SETTLED = "settled"


class StageValue(str, Enum):
    TRUE = "true"
    FALSE = "false"
    UNKNOWN = "unknown"


class BoundaryDecision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    NO_CONTROL = "no_control"
    UNKNOWN = "unknown"


class ContainmentOutcome(str, Enum):
    HARNESS_CONTAINED = "harness_contained"
    HARM_COMMITTED = "harm_committed"
    UNRESOLVED = "unresolved"


def validate_evidence_refs(refs: tuple[str, ...]) -> None:
    for ref in refs:
        path = Path(ref)
        if not ref.strip() or path.is_absolute() or ".." in path.parts:
            raise ValueError("evidence references must be relative to the gate artifact")


@dataclass(frozen=True)
class InvariantObservation:
    invariant_id: str
    held: StageValue
    evidence_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class UnsafeStateObservation:
    state_id: str
    unsafe: bool
    introduced: StageValue = StageValue.UNKNOWN
    retrievable: StageValue = StageValue.UNKNOWN
    persisted: StageValue = StageValue.UNKNOWN
    influenced_episode: StageValue = StageValue.UNKNOWN
    modules_reached: tuple[HarnessModule, ...] = ()
    evidence_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class UtilityObservation:
    opportunity_id: str
    completed: StageValue
    evidence_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExactOverwriteResult:
    """One isolated same-ID untrusted-memory write attempt."""

    state_id: str
    write_proposed: bool
    write_attempted: bool
    write_completed: bool
    write_authorized: bool | None
    trusted_body_preserved: bool | None
    advbench_body_present_under_trusted_id: bool | None
    advbench_body_quarantined: bool | None
    benign_control_succeeded: bool | None
    evidence_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class NearShadowResult:
    """One native retrieval observation after an all-record shadow insertion."""

    state_id: str
    shadow_state_id: str
    trusted_rank: int | None
    advbench_rank: int | None
    provenance_preserved: bool | None
    evidence_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class CorpusPressurePoint:
    """One cumulative whole-document pressure stage on a disposable snapshot copy."""

    requested_whitespace_tokens: int
    actual_whitespace_tokens: int
    documents_added: int
    source_ids: tuple[str, ...]
    anchors_present: int
    anchors_unchanged: int
    physical_retention: float | None
    recall_at_1: float | None
    recall_at_3: float | None
    mean_reciprocal_rank: float | None
    pressure_control_passed: bool | None
    evaluation_status: str = "evaluated"
    reason: str = ""
    evidence_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class IncidentObservation:
    incident_id: str
    recovery_target: str
    fault_injected: StageValue
    recovery_opportunity: StageValue
    state_restored: StageValue
    evidence_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class ArchiveLineageObservation:
    state_id: str
    records: tuple[LogicalTransitionRecord, ...]
    available: bool
    evidence_refs: tuple[str, ...] = ()


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


@dataclass(frozen=True)
class EvidenceCellObservation:
    cell_id: str
    stratum: EvidenceStratum
    status: SafetyStatus
    administered: bool
    oracle_complete: bool
    violation: bool | None
    evidence_refs: tuple[str, ...]
    component_outcomes: tuple[SafetyStatus, ...] = ()
    reason: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.stratum, EvidenceStratum):
            raise TypeError("evidence cell requires an EvidenceStratum")
        if not isinstance(self.status, SafetyStatus):
            raise TypeError("evidence cell requires a SafetyStatus")
        if type(self.administered) is not bool or type(self.oracle_complete) is not bool:
            raise TypeError("administered and oracle_complete must be booleans")
        if self.violation is not None and type(self.violation) is not bool:
            raise TypeError("evidence cell violation must be bool or None")
        if not all(isinstance(item, SafetyStatus) for item in self.component_outcomes):
            raise TypeError("evidence cell component outcomes require SafetyStatus values")
        validate_evidence_refs(self.evidence_refs)
        if self.status in {SafetyStatus.PASS, SafetyStatus.FAIL} and self.violation is None:
            raise ValueError("terminal evidence cell requires a violation value")
        if self.status in {SafetyStatus.PASS, SafetyStatus.FAIL} and not self.evidence_refs:
            raise ValueError("terminal evidence cell requires direct evidence references")


@dataclass(frozen=True)
class ProbeObservation:
    family_id: str
    snapshot: SnapshotRef
    endpoint: ProbeEndpoint
    arm: EvaluationArm
    runtime_kind: RuntimeKind
    status: SafetyStatus
    administered: bool
    cells: tuple[EvidenceCellObservation, ...]
    statuses: ProbeStatuses = field(default_factory=ProbeStatuses)
    exposure: SafetyExposure = SafetyExposure.EXPOSED
    invariants: tuple[InvariantObservation, ...] = ()
    unsafe_states: tuple[UnsafeStateObservation, ...] = ()
    utilities: tuple[UtilityObservation, ...] = ()
    admission_exact_results: tuple[ExactOverwriteResult, ...] = ()
    admission_shadow_results: tuple[NearShadowResult, ...] = ()
    admission_behavior_target: str | None = None
    collapse_corpus_id: str | None = None
    collapse_corpus_documents_available: int = 0
    collapse_corpus_whitespace_tokens_available: int = 0
    collapse_ordinary_memory_records: int = 0
    collapse_ordinary_memory_bytes: int = 0
    corpus_pressure_points: tuple[CorpusPressurePoint, ...] = ()
    incidents: tuple[IncidentObservation, ...] = ()
    archive_lineage: tuple[ArchiveLineageObservation, ...] = ()
    receipts: tuple[NativeReceipt, ...] = ()
    model_provenance: tuple[LiveCallProvenance, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    reason: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.statuses, ProbeStatuses):
            raise TypeError("probe observation requires ProbeStatuses")
        if not self.cells or len({cell.cell_id for cell in self.cells}) != len(self.cells):
            raise ValueError("probe observation requires unique evidence cells")
        expected_role = {
            ProbeEndpoint.ACTIVE: SnapshotRole.ACTIVE,
            ProbeEndpoint.CANDIDATE: SnapshotRole.CANDIDATE,
            ProbeEndpoint.SETTLED: SnapshotRole.ACTIVE,
        }[self.endpoint]
        if self.snapshot.role is not expected_role:
            raise ValueError("probe endpoint must match snapshot role")
        validate_evidence_refs(self.evidence_refs)
        real = [cell for cell in self.cells if cell.stratum is EvidenceStratum.REAL_EPISODE]
        if (
            self.runtime_kind is RuntimeKind.MODEL_MEDIATED
            and any(cell.status in {SafetyStatus.PASS, SafetyStatus.FAIL} for cell in real)
            and not self.model_provenance
        ):
            raise ValueError("model-mediated real episode requires model provenance")
