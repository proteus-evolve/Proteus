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
    PermissionCapabilityState,
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
    build_cache_root: Path | None = None
    runtime_identity: str = ""
    settled_root: Path | None = None


@runtime_checkable
class PermissionPolicyAdapter(Protocol):
    name: str
    kind: RuntimeKind

    @property
    def declared_supported_case_ids(self) -> frozenset[str]: ...

    def live_call_cap(self, case_spec: PermissionPolicyCaseSpec) -> int: ...

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


@dataclass(frozen=True)
class UnsupportedPermissionPolicyAdapter:
    """An honest declaration for harnesses without a native permission boundary."""

    name: str
    kind: RuntimeKind
    missing_requirement: str

    @property
    def declared_supported_case_ids(self) -> frozenset[str]:
        return frozenset()

    def capability(
        self,
        case_spec: PermissionPolicyCaseSpec,
        snapshot_context: PermissionSnapshotContext,
    ) -> PermissionCaseCapability:
        del case_spec, snapshot_context
        return PermissionCaseCapability(
            PermissionCapabilityState.UNSUPPORTED,
            native_mechanism="",
            missing_requirement=self.missing_requirement,
        )

    def live_call_cap(self, case_spec: PermissionPolicyCaseSpec) -> int:
        del case_spec
        return 0

    def bind(
        self,
        case_spec: PermissionPolicyCaseSpec,
        snapshot_context: PermissionSnapshotContext,
    ) -> None:
        del case_spec, snapshot_context

    def administer(
        self,
        binding: NativePermissionBinding,
        operation_spec: PermissionOperationSpec,
        channel: LiveModelChannel | None,
    ) -> NativePermissionTrace:
        del binding, operation_spec, channel
        raise RuntimeError("unsupported permission capability cannot be administered")

    def observe_canary(
        self,
        binding: NativePermissionBinding,
        operation_spec: PermissionOperationSpec,
    ) -> CanaryObservation:
        del binding, operation_spec
        raise RuntimeError("unsupported permission capability has no canary")
