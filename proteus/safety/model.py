"""Typed contracts for post-run, zero-interference safety audits."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Protocol

from proteus.core.adapter import ActionEvent, HarnessAdapter, Surface

TAXONOMY_VERSION = "proteus-safety-taxonomy/1"
CANONICAL_PHASES = frozenset({"observe", "propose", "act", "reflect"})


class AuditStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    NOT_EVALUATED = "not_evaluated"
    INVALID = "invalid"
    ERROR = "error"


class Exposure(str, Enum):
    EXPOSED = "exposed"
    NOT_EXPOSED = "not_exposed"
    UNKNOWN = "unknown"


class AuditMode(str, Enum):
    ARTIFACT = "artifact"
    CONTAINED_REPLAY = "contained_replay"
    MATCHED_REPLAY = "matched_replay"


class CausalStatus(str, Enum):
    ESTABLISHED = "established"
    CORRELATED = "correlated"
    NOT_EVALUATED = "not_evaluated"


@dataclass(frozen=True)
class AuditTaxonomy:
    target: str
    scope: str
    initiating_source: str
    episode_phases: tuple[str, ...]
    evolution_stages: tuple[str, ...]
    failure_mode: str
    evidence_authority: str
    evidence_method: str

    def __post_init__(self) -> None:
        required = (
            self.target,
            self.scope,
            self.initiating_source,
            self.failure_mode,
            self.evidence_authority,
            self.evidence_method,
        )
        if not all(value.strip() for value in required):
            raise ValueError("audit taxonomy fields must be non-empty")
        invalid = set(self.episode_phases) - CANONICAL_PHASES
        if invalid:
            raise ValueError(f"invalid episode phase: {sorted(invalid)}")
        if not self.evolution_stages:
            raise ValueError("audit taxonomy needs at least one evolution stage")


@dataclass(frozen=True)
class AuditObservation:
    attempted: bool | None = None
    decision_allowed: bool | None = None
    state_changed: bool | None = None
    safety_invariant_violated: bool | None = None
    downstream_harm_observed: bool | None = None
    persisted_or_propagated: bool | None = None
    detected_or_contained: bool | None = None
    repaired_or_rolled_back: bool | None = None
    utility_preserved: bool | None = None
    causal_status: CausalStatus = CausalStatus.NOT_EVALUATED


def validate_evidence_refs(refs: tuple[str, ...]) -> None:
    for ref in refs:
        path = Path(ref)
        if (
            not ref.strip()
            or path == Path(".")
            or path.is_absolute()
            or ".." in path.parts
        ):
            raise ValueError("evidence references must be relative to the audit root")


@dataclass(frozen=True)
class AuditAssessment:
    status: AuditStatus
    exposure: Exposure = Exposure.UNKNOWN
    observed_behavior: tuple[str, ...] = ()
    failures: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    observation: AuditObservation = field(default_factory=AuditObservation)
    error: str = ""

    def __post_init__(self) -> None:
        validate_evidence_refs(self.evidence_refs)
        if self.status is AuditStatus.PASS and self.failures:
            raise ValueError("passing assessments cannot contain failures")
        if self.status is AuditStatus.FAIL and not self.failures:
            raise ValueError("failed assessments require concrete failures")
        if self.status in (AuditStatus.PASS, AuditStatus.FAIL) and not self.evidence_refs:
            raise ValueError("pass/fail assessments require evidence references")
        if self.status is AuditStatus.ERROR and not self.error:
            raise ValueError("error assessments require error text")
        if self.status is not AuditStatus.ERROR and self.error:
            raise ValueError("only error assessments may carry error text")

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class AuditContext:
    audit_root: Path
    evidence_dir: Path
    run_id: str
    adapter_name: str
    arm: str
    seed: int
    episode: int
    snapshot_root: Path
    surfaces: tuple[Surface, ...]
    events: tuple[ActionEvent, ...]
    self_assessments: tuple[str, ...]


@dataclass(frozen=True)
class AuditResult:
    taxonomy_version: str
    suite: str
    suite_version: str
    case_id: str
    run_id: str
    adapter: str
    arm: str
    seed: int
    episode: int
    taxonomy: AuditTaxonomy
    status: AuditStatus
    exposure: Exposure
    expected_behavior: tuple[str, ...]
    observed_behavior: tuple[str, ...]
    failures: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    observation: AuditObservation
    self_assessments: tuple[str, ...]
    error: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class AuditCase(Protocol):
    case_id: str
    taxonomy: AuditTaxonomy
    expected_behavior: tuple[str, ...]

    def evaluate(self, context: AuditContext) -> AuditAssessment: ...


class AuditSuite(Protocol):
    name: str
    version: str

    def cases(
        self, adapter: HarnessAdapter, surfaces: Sequence[Surface]
    ) -> Sequence[AuditCase]: ...


def build_result(
    *,
    suite: AuditSuite,
    case: AuditCase,
    context: AuditContext,
    assessment: AuditAssessment,
) -> AuditResult:
    """Attach stable suite/run identity to one case-local assessment."""
    return AuditResult(
        taxonomy_version=TAXONOMY_VERSION,
        suite=suite.name,
        suite_version=suite.version,
        case_id=case.case_id,
        run_id=context.run_id,
        adapter=context.adapter_name,
        arm=context.arm,
        seed=context.seed,
        episode=context.episode,
        taxonomy=case.taxonomy,
        status=assessment.status,
        exposure=assessment.exposure,
        expected_behavior=case.expected_behavior,
        observed_behavior=assessment.observed_behavior,
        failures=assessment.failures,
        evidence_refs=assessment.evidence_refs,
        observation=assessment.observation,
        self_assessments=context.self_assessments,
        error=assessment.error,
    )
