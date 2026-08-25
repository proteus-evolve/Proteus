"""Plug-in contracts for future module-first harness-safety case suites."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from proteus.core.adapter import ActionEvent
from proteus.core.snapshot import SnapshotRef
from proteus.safety.evidence import ProbeEndpoint, ProbeObservation
from proteus.safety.live import LiveModelChannel
from proteus.safety.taxonomy import (
    EvaluationArm,
    EvidenceStratum,
    HarnessSafetyProfile,
    SafetyCaseFamilyDefinition,
)


@runtime_checkable
class HarnessSafetyCaseSuite(Protocol):
    name: str
    version: str

    def definitions(self) -> Sequence[SafetyCaseFamilyDefinition]: ...


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
