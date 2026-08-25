"""Declared-cell scheduling for controller-owned activation safety."""

from __future__ import annotations

import importlib
import json
import shutil
from dataclasses import asdict, is_dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Callable

from proteus.core.activation import CandidateGateContext, CandidateGateResult
from proteus.core.episode import eval_history_path
from proteus.core.snapshot import SnapshotRef, SnapshotRole
from proteus.safety.evidence import EvidenceCellObservation, ProbeEndpoint, ProbeObservation
from proteus.safety.indicators import MatchedFamilyObservations, derive_indicator_profile
from proteus.safety.live import LiveModelChannel
from proteus.safety.phase1_runtime import PHASE1_EXECUTORS, Phase1ExecutionRequest
from proteus.safety.plugins import CandidateSafetyContext
from proteus.safety.policy import evaluate_safety_policy, required_outcome
from proteus.safety.publication import AtomicGatePublication
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
    unsupported = set(family_ids) - set(PHASE1_EXECUTORS)
    if unsupported:
        raise ValueError(f"no core executor for safety families: {sorted(unsupported)}")
    return value, definitions


def _json_value(value):
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return {key: _json_value(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(_json_value(value), indent=1, sort_keys=True), encoding="utf-8"
    )
    temporary.replace(path)


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


def _close_channel(channel: LiveModelChannel | None) -> None:
    if channel is None:
        return
    close = getattr(channel, "close", None)
    if callable(close):
        close()


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
        if cell.violation is not None:
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
    ) -> None:
        self._adapter = adapter
        self._definitions = definitions
        self._controller_root = controller_root
        self._safety_model = safety_model
        self._channel_factory = channel_factory

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
                self._safety_model, f"{cell_id}.{endpoint.value}"
            )
        try:
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
            _write_json(staging / "controller" / "lineage.json", lineage)
            pairs: list[MatchedFamilyObservations] = []
            flat: list[ProbeObservation] = []
            for definition in self._definitions:
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
                pair = MatchedFamilyObservations(active, candidate)
                pairs.append(pair)
                flat.extend((active, candidate))
                _write_json(
                    staging / "families" / definition.family_id / "active.json", active
                )
                _write_json(
                    staging / "families" / definition.family_id / "candidate.json",
                    candidate,
                )
            profile = derive_indicator_profile(tuple(pairs))
            decision = evaluate_safety_policy(profile)
            (staging / "results.jsonl").write_text(
                "".join(
                    json.dumps(_json_value(item), sort_keys=True) + "\n" for item in flat
                ),
                encoding="utf-8",
            )
            _write_json(staging / "indicators.json", profile.to_dict())
            _write_json(
                staging / "decision.json",
                {
                    **decision.to_dict(),
                    "run_id": context.run_id,
                    "episode": context.episode,
                    "runtime": _runtime_for(self._adapter).name,
                    "families": {
                        family.family_id: family.candidate_status.value
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
    if runtime.kind is RuntimeKind.MODEL_MEDIATED and not safety_model:
        raise ValueError("model-mediated safety runtime requires --safety-model")
    if runtime.kind is RuntimeKind.DETERMINISTIC and safety_model:
        raise ValueError("deterministic safety runtime does not use --safety-model")
    first_adapter = [preflight_adapter]
    root = Path(controller_root)

    def factory(_run_id: str):
        adapter = first_adapter.pop() if first_adapter else adapter_factory()
        return GateRunner(
            adapter=adapter,
            definitions=definitions,
            controller_root=root,
            safety_model=safety_model,
            channel_factory=channel_factory,
        )

    return factory
