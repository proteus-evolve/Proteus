"""Universal native primitives for activation-time harness safety."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from proteus.core.adapter import ActionEvent
from proteus.core.snapshot import SnapshotRef, SnapshotRole
from proteus.safety.live import LiveCallProvenance, LiveModelChannel

if TYPE_CHECKING:
    from proteus.safety.plugins import CandidateSafetyContext


class RuntimeKind(str, Enum):
    DETERMINISTIC = "deterministic"
    MODEL_MEDIATED = "model_mediated"


def _validate_id(label: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip() or "/" in value or "\\" in value:
        raise ValueError(f"{label} must be a non-empty path-free ID")


@dataclass(frozen=True)
class MemoryStateRequest:
    state_id: str
    body: str
    unsafe: bool

    def __post_init__(self) -> None:
        _validate_id("state ID", self.state_id)
        if not isinstance(self.body, str):
            raise TypeError("memory body must be text")


@dataclass(frozen=True)
class MemoryFaultRequest:
    state_id: str
    operation: str

    def __post_init__(self) -> None:
        _validate_id("state ID", self.state_id)


@dataclass(frozen=True)
class NativeReceipt:
    operation_id: str
    proposed: bool
    attempted: bool
    completed: bool
    result_delivered: bool
    authorized: bool | None
    evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.operation_id.strip():
            raise ValueError("receipt operation ID must be non-empty")


@dataclass(frozen=True)
class OrdinaryMemoryRecord:
    """A trusted ordinary memory record exposed by an adapter-native inventory."""

    state_id: str
    body: str
    source: str
    trust: str
    lookup_query: str
    evidence_ref: str

    def __post_init__(self) -> None:
        _validate_id("ordinary memory state ID", self.state_id)
        if self.source != "ordinary" or self.trust != "trusted":
            raise ValueError("ordinary memory records must be ordinary and trusted")
        if not self.lookup_query.strip() or not self.evidence_ref.strip():
            raise ValueError("ordinary memory records require query and evidence")


@dataclass(frozen=True)
class MemoryQueryHit:
    """One ordered result from an adapter's ordinary memory resolver."""

    state_id: str
    rank: int
    source: str
    trust: str
    evidence_ref: str

    def __post_init__(self) -> None:
        _validate_id("memory query state ID", self.state_id)
        if type(self.rank) is not int or self.rank < 1:
            raise ValueError("memory query rank must be a positive integer")
        if not self.source.strip() or not self.trust.strip() or not self.evidence_ref.strip():
            raise ValueError("memory query hits require provenance and evidence")


@dataclass(frozen=True)
class SafetyEpisodeResult:
    terminal: bool
    events: tuple[ActionEvent, ...]
    receipts: tuple[NativeReceipt, ...]
    model_provenance: tuple[LiveCallProvenance, ...]
    evidence_refs: tuple[str, ...]
    error: str = ""


@dataclass(frozen=True)
class LogicalTransitionRecord:
    active: SnapshotRef
    candidate: SnapshotRef
    activated: bool | None
    decision_ref: str

    def __post_init__(self) -> None:
        if self.active.role is not SnapshotRole.ACTIVE:
            raise ValueError("logical transition active ref requires the active role")
        if self.candidate.role is not SnapshotRole.CANDIDATE:
            raise ValueError("logical transition candidate ref requires the candidate role")
        if self.active.run_id != self.candidate.run_id:
            raise ValueError("logical transition refs require one run ID")


@runtime_checkable
class HarnessSafetyRuntime(Protocol):
    name: str
    kind: RuntimeKind

    def introduce_memory(
        self, request: MemoryStateRequest, context: CandidateSafetyContext
    ) -> NativeReceipt: ...

    def read_memory(
        self, state_id: str, context: CandidateSafetyContext
    ) -> NativeReceipt: ...

    def inject_memory_fault(
        self, request: MemoryFaultRequest, context: CandidateSafetyContext
    ) -> NativeReceipt: ...

    def run_safety_episode(
        self,
        prompts: Mapping[str, str],
        context: CandidateSafetyContext,
        channel: LiveModelChannel | None,
    ) -> SafetyEpisodeResult: ...

    def memory_oracle(
        self, state_id: str, expected_body: str, context: CandidateSafetyContext
    ) -> bool | None: ...


@runtime_checkable
class OrdinaryMemoryRuntime(Protocol):
    """Optional native memory inventory and ranked-query capability.

    A harness that cannot expose this through its normal memory route must omit the
    capability and receive ``not_evaluated`` for retrieval-based family components.
    """

    def enumerate_ordinary_memory(
        self, context: CandidateSafetyContext
    ) -> tuple[OrdinaryMemoryRecord, ...]: ...

    def query_memory(
        self, query: str, limit: int, context: CandidateSafetyContext
    ) -> tuple[MemoryQueryHit, ...] | None: ...
