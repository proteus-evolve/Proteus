"""Harness-specific native adapter contract for permission-policy evidence."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
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
from proteus.safety.tool_catalog import NativeToolCatalog, native_tool_catalog_evidence_is_local


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
    """An honest declaration for harnesses without a native permission boundary.

    Some text-action harnesses still have a complete callable inventory: their ordinary
    runtime exposes *no* native tool schemas.  ``native_tool_catalog_loader_id`` records
    that fact in controller-owned evidence without treating authored ``tools/*.py`` files
    as executable callables.
    """

    name: str
    kind: RuntimeKind
    missing_requirement: str
    native_tool_catalog_loader_id: str = ""
    native_tool_catalog_observation: str = ""
    _native_tool_catalogs: dict[SnapshotRef, NativeToolCatalog] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )
    _native_tool_catalog_reasons: dict[SnapshotRef, str] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )

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

    def collect_native_tool_catalog(
        self, context: PermissionSnapshotContext
    ) -> NativeToolCatalog | None:
        """Record a verified empty native-tool catalog when this runtime has none.

        The absence is a runtime fact, not a source-tree scan.  Adapters which cannot
        make that claim leave ``native_tool_catalog_loader_id`` empty and remain
        not-evaluated for catalog purposes.
        """
        if not self.native_tool_catalog_loader_id:
            self._native_tool_catalog_reasons[context.snapshot] = (
                "native_tool_catalog_unavailable"
            )
            return None
        cached = self._native_tool_catalogs.get(context.snapshot)
        if cached is not None and native_tool_catalog_evidence_is_local(
            cached,
            artifact_root=context.artifact_root,
            evidence_dir=context.evidence_dir,
        ):
            return cached
        if cached is not None:
            self._native_tool_catalogs.pop(context.snapshot, None)
            self._native_tool_catalog_reasons.pop(context.snapshot, None)
        try:
            catalog_path = context.evidence_dir / "native-tool-catalog.json"
            relative_ref = catalog_path.relative_to(context.artifact_root).as_posix()
        except ValueError:
            self._native_tool_catalog_reasons[context.snapshot] = (
                "native_tool_catalog_evidence_outside_artifact_root"
            )
            return None
        try:
            catalog_path.parent.mkdir(parents=True, exist_ok=True)
            catalog_path.write_text(
                json.dumps(
                    {
                        "adapter": self.name,
                        "kind": self.kind.value,
                        "snapshot": context.snapshot.to_dict(),
                        "loader_id": self.native_tool_catalog_loader_id,
                        "observation": self.native_tool_catalog_observation,
                        "ordinary_native_tool_schemas": [],
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            catalog = NativeToolCatalog(
                snapshot=context.snapshot,
                loader_id=self.native_tool_catalog_loader_id,
                tools=(),
                raw_catalog_ref=relative_ref,
            )
        except (OSError, ValueError) as exc:
            self._native_tool_catalog_reasons[context.snapshot] = (
                f"native_tool_catalog_evidence_error:{type(exc).__name__}"
            )
            return None
        self._native_tool_catalogs[context.snapshot] = catalog
        self._native_tool_catalog_reasons.pop(context.snapshot, None)
        return catalog

    def native_tool_catalog_reason(self, snapshot: SnapshotRef) -> str:
        """Return an exact absence reason, or empty text after a complete observation."""
        if snapshot in self._native_tool_catalogs:
            return ""
        return self._native_tool_catalog_reasons.get(
            snapshot,
            "" if self.native_tool_catalog_loader_id else "native_tool_catalog_unavailable",
        )

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
