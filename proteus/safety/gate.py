"""Minimal controller shell that runs the core Phase 1 family functions."""

from __future__ import annotations

import importlib
import json
import shutil
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Callable

from proteus.core.activation import CandidateGateContext, CandidateGateResult
from proteus.core.episode import eval_history_path
from proteus.core.snapshot import SnapshotRef, SnapshotRole
from proteus.safety.evidence import ProbeObservation
from proteus.safety.phase1_runtime import PHASE1_EXECUTORS, Phase1ExecutionRequest
from proteus.safety.plugins import CandidateSafetyContext
from proteus.safety.runtime import (
    HarnessSafetyRuntime,
    LogicalTransitionRecord,
    RuntimeKind,
)
from proteus.safety.taxonomy import SafetyCaseFamilyDefinition, SafetyStatus


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


class _Phase1Gate:
    def __init__(
        self,
        *,
        adapter,
        definitions: tuple[SafetyCaseFamilyDefinition, ...],
        controller_root: Path,
    ) -> None:
        self._adapter = adapter
        self._definitions = definitions
        self._controller_root = controller_root

    def evaluate(self, context: CandidateGateContext) -> CandidateGateResult:
        gate_root = (
            self._controller_root
            / "safety-gates"
            / context.run_id
            / f"episode-{context.episode:03d}"
        )
        gate_root.mkdir(parents=True, exist_ok=True)
        lineage = _load_lineage(self._controller_root, context)
        _write_json(gate_root / "controller" / "lineage.json", lineage)
        observations: list[ProbeObservation] = []
        runtime = self._adapter.safety_runtime()
        for definition in self._definitions:
            trial_root = gate_root / "trials" / definition.family_id
            snapshot_root = trial_root / "harness"
            if trial_root.exists():
                shutil.rmtree(trial_root)
            shutil.copytree(context.candidate_root, snapshot_root)
            safety_context = CandidateSafetyContext(
                run_id=context.run_id,
                episode=context.episode,
                adapter_name=self._adapter.name,
                snapshot=context.candidate,
                snapshot_root=snapshot_root,
                trial_root=trial_root,
                evidence_dir=gate_root / "evidence" / definition.family_id,
                events=context.events,
                lineage=lineage,
            )
            observation = PHASE1_EXECUTORS[definition.family_id](
                Phase1ExecutionRequest(
                    definition=definition,
                    runtime=runtime,
                    context=safety_context,
                    channel=None,
                )
            )
            observations.append(observation)
            _write_json(
                gate_root / "families" / f"{definition.family_id}.json", observation
            )
        results_path = gate_root / "results.jsonl"
        results_path.write_text(
            "".join(json.dumps(_json_value(item), sort_keys=True) + "\n"
                    for item in observations),
            encoding="utf-8",
        )
        statuses = {item.family_id: item.status.value for item in observations}
        allowed = bool(observations) and all(
            item.status is SafetyStatus.PASS for item in observations
        )
        decision = {
            "run_id": context.run_id,
            "episode": context.episode,
            "runtime": runtime.name,
            "runtime_kind": runtime.kind.value,
            "families": statuses,
            "allowed": allowed,
            "status": "pass" if allowed else "fail",
        }
        decision_path = gate_root / "decision.json"
        _write_json(decision_path, decision)
        decision_ref = str(decision_path.relative_to(self._controller_root))
        return CandidateGateResult(
            allowed=allowed,
            status="pass" if allowed else "fail",
            decision_ref=decision_ref,
        )


def build_candidate_gate_factory(
    *, adapter_factory: Callable[[], object], suite_spec: str, safety_model: str,
    controller_root: Path,
):
    """Validate the runtime before output creation and return one gate per run."""
    _, definitions = _load_suite(suite_spec)
    adapter = adapter_factory()
    method = getattr(adapter, "safety_runtime", None)
    if not callable(method):
        raise TypeError(
            f"safety-gated adapter {getattr(adapter, 'name', type(adapter).__name__)!r} "
            "must implement safety_runtime()"
        )
    runtime = method()
    if not isinstance(runtime, HarnessSafetyRuntime):
        raise TypeError("adapter safety_runtime() does not implement HarnessSafetyRuntime")
    if runtime.kind is RuntimeKind.MODEL_MEDIATED and not safety_model:
        raise ValueError("model-mediated safety runtime requires --safety-model")
    if runtime.kind is RuntimeKind.DETERMINISTIC and safety_model:
        raise ValueError("deterministic safety runtime does not use --safety-model")
    root = Path(controller_root)

    def factory(_run_id: str):
        return _Phase1Gate(
            adapter=adapter_factory(), definitions=definitions, controller_root=root
        )

    return factory
