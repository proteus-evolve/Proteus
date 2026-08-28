"""Plug-in contracts for activation-time harness safety."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from proteus.core.adapter import ActionEvent
from proteus.core.snapshot import SnapshotRef
from proteus.safety.evidence import ProbeEndpoint
from proteus.safety.permission_adapter import PermissionPolicyAdapter
from proteus.safety.runtime import HarnessSafetyRuntime, LogicalTransitionRecord
from proteus.safety.taxonomy import SafetyCaseFamilyDefinition


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
    events: tuple[ActionEvent, ...] = ()
    lineage: tuple[LogicalTransitionRecord, ...] = ()
    artifact_root: Path | None = None
    active_root: Path | None = None
    goal_text: str = ""
    endpoint: ProbeEndpoint | None = None
    build_cache_root: Path | None = None
    runtime_identity: str = ""


@runtime_checkable
class CandidateSafetyAdapter(Protocol):
    def safety_runtime(self) -> HarnessSafetyRuntime: ...

    def permission_policy_adapter(self) -> PermissionPolicyAdapter: ...
