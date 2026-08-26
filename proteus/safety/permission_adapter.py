"""Harness-specific native adapter contract for permission-policy evidence."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from proteus.core.snapshot import SnapshotRef
from proteus.safety.live import LiveModelChannel
from proteus.safety.permission_cases import PermissionOperationSpec, PermissionPolicyCaseSpec
from proteus.safety.permission_evidence import (
    CanaryObservation,
    NativePermissionBinding,
    NativePermissionTrace,
    PermissionCaseCapability,
)
from proteus.safety.runtime import RuntimeKind


@dataclass(frozen=True)
class PermissionSnapshotContext:
    snapshot: SnapshotRef
    snapshot_root: Path
    trial_root: Path
    evidence_dir: Path
    artifact_root: Path


@runtime_checkable
class PermissionPolicyAdapter(Protocol):
    name: str
    kind: RuntimeKind

    @property
    def declared_supported_case_ids(self) -> frozenset[str]: ...

    def capability(
        self,
        case_spec: PermissionPolicyCaseSpec,
        snapshot_context: PermissionSnapshotContext,
    ) -> PermissionCaseCapability: ...

    def bind(
        self,
        case_spec: PermissionPolicyCaseSpec,
        snapshot_context: PermissionSnapshotContext,
    ) -> NativePermissionBinding | None: ...

    def administer(
        self,
        binding: NativePermissionBinding,
        operation_spec: PermissionOperationSpec,
        channel: LiveModelChannel | None,
    ) -> NativePermissionTrace: ...

    def observe_canary(
        self,
        binding: NativePermissionBinding,
        operation_spec: PermissionOperationSpec,
    ) -> CanaryObservation: ...
