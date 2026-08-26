"""Declared-cell scheduling for controller-owned activation safety."""

from __future__ import annotations

import importlib
import json
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Callable

from proteus.core.activation import CandidateGateContext, CandidateGateResult
from proteus.core.episode import eval_history_path
from proteus.core.snapshot import SnapshotRef, SnapshotRole
from proteus.safety.evidence import EvidenceCellObservation, ProbeEndpoint, ProbeObservation
from proteus.safety.indicators import MatchedFamilyObservations, derive_indicator_profile
from proteus.safety.live import LiveModelChannel
from proteus.safety.permission_adapter import PermissionPolicyAdapter
from proteus.safety.permission_executor import (
    PairedPermissionPolicyExecutor,
    PermissionSnapshotSource,
    TransitionPermissionRequest,
)
from proteus.safety.phase1 import TOOLS_PERMISSION_DRIFT
from proteus.safety.phase1_runtime import PHASE1_EXECUTORS, Phase1ExecutionRequest
from proteus.safety.plugins import CandidateSafetyContext
from proteus.safety.policy import (
    aggregate_required_outcomes,
    evaluate_safety_policy,
    required_outcome,
)
from proteus.safety.publication import AtomicGatePublication, write_json, write_jsonl
from proteus.safety.runtime import HarnessSafetyRuntime, LogicalTransitionRecord, RuntimeKind
from proteus.safety.taxonomy import SafetyCaseFamilyDefinition

LiveChannelFactory = Callable[[str, str], LiveModelChannel]


def _load_suite(spec: str):
    module_name, separator, object_name = spec.partition(":")
    if not separator or not module_name or not object_name:
        raise ValueError("safety suite must use <module>:<object>")
    module = importlib.import_module(module_name)
    value = getattr(module, object_name)
    if isinstance(value, type):
        value = value()
    if not callable(getattr(value, "definitions", None)):
        raise TypeError("safety suite must expose definitions()")
    definitions = tuple(value.definitions())
    if not definitions or not all(
        isinstance(item, SafetyCaseFamilyDefinition) for item in definitions
    ):
        raise TypeError("safety suite definitions must be typed case families")
    family_ids = [item.family_id for item in definitions]
    if len(family_ids) != len(set(family_ids)):
        raise ValueError("safety suite family IDs must be unique")
    permission_definitions = tuple(
        item for item in definitions if item.family_id == TOOLS_PERMISSION_DRIFT.family_id
    )
    if len(permission_definitions) != 1:
        raise ValueError("safety suite must contain one current tools_permission_drift family")
    permission = permission_definitions[0]
    if (
        permission.family_version != "2"
        or permission.permission_cases != TOOLS_PERMISSION_DRIFT.permission_cases
    ):
        raise ValueError("tools_permission_drift must use the current version 2 case catalog")
    memory_ids = {
        item.family_id
        for item in definitions
        if item.family_id != TOOLS_PERMISSION_DRIFT.family_id
    }
    unsupported = memory_ids - set(PHASE1_EXECUTORS)
    if unsupported:
        raise ValueError(f"no core executor for safety families: {sorted(unsupported)}")
    return value, definitions


def _load_lineage(
    controller_root: Path, context: CandidateGateContext
) -> tuple[LogicalTransitionRecord, ...]:
    run_root = controller_root / "runs" / context.run_id
    path = eval_history_path(run_root)
    prior: list[LogicalTransitionRecord] = []
    if context.episode > 1:
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(rows, list):
                return ()
            for episode, row in enumerate(rows, 1):
                if episode >= context.episode or not isinstance(row, dict):
                    continue
                activated = row.get("activated", row.get("accepted"))
                if type(activated) is not bool:
                    return ()
                prior.append(
                    LogicalTransitionRecord(
                        active=SnapshotRef(context.run_id, episode - 1, SnapshotRole.ACTIVE),
                        candidate=SnapshotRef(
                            context.run_id, episode, SnapshotRole.CANDIDATE
                        ),
                        activated=activated,
                        decision_ref=str(row.get("decision_ref", "task-selection")),
                    )
                )
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return ()
        if len(prior) != context.episode - 1:
            return ()
    return tuple(prior)


def _runtime_for(adapter) -> HarnessSafetyRuntime:
    method = getattr(adapter, "safety_runtime", None)
    if not callable(method):
        raise TypeError(
            f"safety-gated adapter {getattr(adapter, 'name', type(adapter).__name__)!r} "
            "must implement safety_runtime()"
        )
    runtime = method()
    if not isinstance(runtime, HarnessSafetyRuntime):
        raise TypeError("adapter safety_runtime() does not implement HarnessSafetyRuntime")
    return runtime


def _permission_adapter_for(adapter) -> PermissionPolicyAdapter:
    method = getattr(adapter, "permission_policy_adapter", None)
    if not callable(method):
        raise TypeError(
            f"safety-gated adapter {getattr(adapter, 'name', type(adapter).__name__)!r} "
            "must implement permission_policy_adapter()"
        )
    permission_adapter = method()
    if not isinstance(permission_adapter, PermissionPolicyAdapter):
        raise TypeError(
            "adapter permission_policy_adapter() does not implement PermissionPolicyAdapter"
        )
    return permission_adapter


def _close_channel(channel: LiveModelChannel | None) -> None:
    if channel is None:
        return
    channel.close()


def _project_cell_evidence(
    observation: ProbeObservation,
    *,
    definition: SafetyCaseFamilyDefinition,
    endpoint: ProbeEndpoint,
    artifact_root: Path,
) -> ProbeObservation:
    projected: list[EvidenceCellObservation] = []
    projected_refs: list[str] = []
    for cell in observation.cells:
        target_root = (
            artifact_root
            / "evidence"
            / definition.family_id
            / endpoint.value
            / cell.cell_id
        )
        refs: list[str] = []
        for index, ref in enumerate(cell.evidence_refs, 1):
            source = artifact_root / ref
            try:
                source.relative_to(artifact_root)
            except ValueError as exc:
                raise ValueError("safety evidence reference escapes the gate artifact") from exc
            if not source.is_file():
                raise ValueError(f"missing direct safety evidence: {ref}")
            target_root.mkdir(parents=True, exist_ok=True)
            target = target_root / f"{index:02d}-{source.name}"
            shutil.copy2(source, target)
            projected_ref = target.relative_to(artifact_root).as_posix()
            refs.append(projected_ref)
            projected_refs.append(projected_ref)
        projected.append(replace(cell, evidence_refs=tuple(refs)))
    return replace(
        observation,
        cells=tuple(projected),
        evidence_refs=tuple(dict.fromkeys(projected_refs)),
    )


def _validate_observation(
    observation: object,
    *,
    definition: SafetyCaseFamilyDefinition,
    snapshot: SnapshotRef,
    endpoint: ProbeEndpoint,
    runtime: HarnessSafetyRuntime,
    artifact_root: Path,
) -> ProbeObservation:
    if not isinstance(observation, ProbeObservation):
        raise TypeError("core safety executor returned malformed evidence")
    if (
        observation.family_id != definition.family_id
        or observation.snapshot != snapshot
        or observation.endpoint is not endpoint
        or observation.runtime_kind is not runtime.kind
    ):
        raise ValueError("core safety executor returned mismatched evidence identity")
    declared = tuple(
        (cell.cell_id, cell.arm, cell.stratum) for cell in definition.declared_cells
    )
    returned = tuple(
        (cell.cell_id, observation.arm, cell.stratum) for cell in observation.cells
    )
    if returned != declared:
        raise ValueError("core safety executor did not return the exact declared cells")
    for cell in observation.cells:
        if cell.component_outcomes:
            expected = aggregate_required_outcomes(*cell.component_outcomes)
            if cell.status is not expected:
                raise ValueError("safety cell status contradicts component outcomes")
        elif cell.violation is not None:
            expected = required_outcome(
                administered=cell.administered,
                oracle_complete=cell.oracle_complete,
                violation=cell.violation,
            )
            if cell.status is not expected:
                raise ValueError("safety cell status contradicts administered evidence")
        for ref in cell.evidence_refs:
            path = artifact_root / ref
            try:
                path.relative_to(artifact_root)
            except ValueError as exc:
                raise ValueError("safety evidence reference escapes the gate artifact") from exc
            if not path.is_file():
                raise ValueError(f"missing direct safety evidence: {ref}")
    return observation


class GateRunner:
    def __init__(
        self,
        *,
        adapter,
        definitions: tuple[SafetyCaseFamilyDefinition, ...],
        controller_root: Path,
        safety_model: str,
        channel_factory: LiveChannelFactory | None,
        permission_adapter: PermissionPolicyAdapter | None = None,
        permission_executor: PairedPermissionPolicyExecutor | None = None,
    ) -> None:
        self._adapter = adapter
        self._definitions = definitions
        self._controller_root = controller_root
        self._safety_model = safety_model
        self._channel_factory = channel_factory
        self._permission_adapter = permission_adapter
        self._permission_executor = permission_executor or PairedPermissionPolicyExecutor()

    def _collect_family(
        self,
        *,
        definition: SafetyCaseFamilyDefinition,
        endpoint: ProbeEndpoint,
        context: CandidateGateContext,
        lineage: tuple[LogicalTransitionRecord, ...],
        artifact_root: Path,
    ) -> ProbeObservation:
        source = (
            context.active_root
            if endpoint is ProbeEndpoint.ACTIVE
            else context.candidate_root
        )
        snapshot = context.active if endpoint is ProbeEndpoint.ACTIVE else context.candidate
        trial_root = artifact_root / "trials" / definition.family_id / endpoint.value
        snapshot_root = trial_root / "harness"
        shutil.copytree(source, snapshot_root)
        active_root = trial_root.parent / f".{endpoint.value}-logical-active" / "harness"
        shutil.copytree(context.active_root, active_root)
        safety_context = CandidateSafetyContext(
            run_id=context.run_id,
            episode=context.episode,
            adapter_name=self._adapter.name,
            snapshot=snapshot,
            snapshot_root=snapshot_root,
            trial_root=trial_root,
            evidence_dir=trial_root / "raw-evidence",
            events=context.events,
            lineage=lineage,
            artifact_root=artifact_root,
            active_root=active_root,
        )
        runtime = _runtime_for(self._adapter)
        has_real_episode = any(
            cell.stratum.value == "real_episode" for cell in definition.declared_cells
        )
        channel = None
        if runtime.kind is RuntimeKind.MODEL_MEDIATED and has_real_episode:
            if self._channel_factory is None:
                raise ValueError("model-mediated safety runtime has no live channel factory")
            cell_id = next(
                cell.cell_id
                for cell in definition.declared_cells
                if cell.stratum.value == "real_episode"
            )
            channel = self._channel_factory(
                self._safety_model,
                (
                    f"{context.run_id}.episode-{context.episode:03d}."
                    f"{cell_id}.{endpoint.value}"
                ),
            )
            if not callable(getattr(channel, "close", None)):
                raise TypeError("live channel factory must implement LiveModelChannel")
        try:
            if channel is not None and not isinstance(channel, LiveModelChannel):
                raise TypeError("live channel factory must implement LiveModelChannel")
            observation = PHASE1_EXECUTORS[definition.family_id](
                Phase1ExecutionRequest(
                    definition=definition,
                    runtime=runtime,
                    context=safety_context,
                    channel=channel,
                )
            )
        finally:
            _close_channel(channel)
            shutil.rmtree(active_root.parent)
        validated = _validate_observation(
            observation,
            definition=definition,
            snapshot=snapshot,
            endpoint=endpoint,
            runtime=runtime,
            artifact_root=artifact_root,
        )
        return _project_cell_evidence(
            validated,
            definition=definition,
            endpoint=endpoint,
            artifact_root=artifact_root,
        )

    def evaluate(self, context: CandidateGateContext) -> CandidateGateResult:
        final_root = (
            self._controller_root
            / "safety-gates"
            / context.run_id
            / f"episode-{context.episode:03d}"
        )
        decision_ref = (final_root / "decision.json").relative_to(
            self._controller_root
        ).as_posix()
        lineage = _load_lineage(self._controller_root, context)
        with AtomicGatePublication(final_root) as publication:
            assert publication.staging_root is not None
            staging = publication.staging_root
            write_json(staging / "controller" / "lineage.json", lineage)
            pairs: list[MatchedFamilyObservations] = []
            results: list[object] = []
            memory_definitions = tuple(
                definition
                for definition in self._definitions
                if definition.family_id != TOOLS_PERMISSION_DRIFT.family_id
            )
            permission_definition = next(
                definition
                for definition in self._definitions
                if definition.family_id == TOOLS_PERMISSION_DRIFT.family_id
            )
            for definition in memory_definitions:
                active = self._collect_family(
                    definition=definition,
                    endpoint=ProbeEndpoint.ACTIVE,
                    context=context,
                    lineage=lineage,
                    artifact_root=staging,
                )
                candidate = self._collect_family(
                    definition=definition,
                    endpoint=ProbeEndpoint.CANDIDATE,
                    context=context,
                    lineage=lineage,
                    artifact_root=staging,
                )
                pair = MatchedFamilyObservations(
                    active, candidate, definition.family_version
                )
                pairs.append(pair)
                results.extend((active, candidate))
                write_json(
                    staging / "families" / definition.family_id / "active.json", active
                )
                write_json(
                    staging / "families" / definition.family_id / "candidate.json",
                    candidate,
                )
            permission_adapter = self._permission_adapter or _permission_adapter_for(
                self._adapter
            )

            def permission_channel_factory(model: str, cell_id: str, cap: int):
                if cap != 2:
                    raise ValueError("permission policy channels require a two-call cap")
                if self._channel_factory is None:
                    return None
                return self._channel_factory(model, cell_id)

            permission = self._permission_executor.execute(
                TransitionPermissionRequest(
                    active=PermissionSnapshotSource(context.active, context.active_root),
                    candidate=PermissionSnapshotSource(
                        context.candidate, context.candidate_root
                    ),
                    case_specs=permission_definition.permission_cases,
                    adapter=permission_adapter,
                    artifact_root=staging,
                    safety_model=self._safety_model,
                    channel_factory=(
                        permission_channel_factory
                        if self._channel_factory is not None
                        else None
                    ),
                )
            )
            results.append(permission)
            profile = derive_indicator_profile(tuple(pairs), permission)
            decision = evaluate_safety_policy(profile)
            write_jsonl(staging / "results.jsonl", results)
            write_json(staging / "indicators.json", profile.to_dict())
            write_json(
                staging / "decision.json",
                {
                    **decision.to_dict(),
                    "run_id": context.run_id,
                    "episode": context.episode,
                    "runtime": _runtime_for(self._adapter).name,
                    "families": {
                        family.family_id: family.terminal_status.value
                        for family in profile.families
                    },
                },
            )
            publication.publish()
        return CandidateGateResult(
            allowed=decision.allowed,
            status=decision.status.value,
            decision_ref=decision_ref,
        )


def build_candidate_gate_factory(
    *,
    adapter_factory: Callable[[], object],
    suite_spec: str,
    safety_model: str,
    controller_root: Path,
    channel_factory: LiveChannelFactory | None = None,
):
    """Preflight only the selected adapter before any sweep output is created."""
    _, definitions = _load_suite(suite_spec)
    preflight_adapter = adapter_factory()
    runtime = _runtime_for(preflight_adapter)
    permission_adapter = _permission_adapter_for(preflight_adapter)
    if runtime.kind is RuntimeKind.MODEL_MEDIATED and not safety_model:
        raise ValueError("model-mediated safety runtime requires --safety-model")
    if runtime.kind is RuntimeKind.DETERMINISTIC and safety_model:
        raise ValueError("deterministic safety runtime does not use --safety-model")
    first_adapter = [preflight_adapter]
    first_permission_adapter = [permission_adapter]
    root = Path(controller_root)

    def factory(_run_id: str):
        adapter = first_adapter.pop() if first_adapter else adapter_factory()
        paired_adapter = (
            first_permission_adapter.pop()
            if first_permission_adapter
            else _permission_adapter_for(adapter)
        )
        return GateRunner(
            adapter=adapter,
            definitions=definitions,
            controller_root=root,
            safety_model=safety_model,
            channel_factory=channel_factory,
            permission_adapter=paired_adapter,
        )

    return factory
