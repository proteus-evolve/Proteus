"""Execute plug-in harness-safety families over completed Proteus trajectories."""

from __future__ import annotations

import json
import shutil
import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from itertools import pairwise
from pathlib import Path

from proteus.core import snapshot
from proteus.core.adapter import HarnessAdapter
from proteus.safety.evaluation import FamilyAssessment, evaluate_family
from proteus.safety.model import CausalStatus
from proteus.safety.plugins import (
    HarnessSafetyAdapter,
    HarnessSafetyCaseSuite,
    HarnessSafetyContext,
    HarnessSafetyEvidence,
    HarnessSafetyEvidenceProvider,
    ModelBehavior,
)
from proteus.safety.runner import (
    _load_sweep,
    _planned_runs,
    _utc_now,
    _validate_completed_sweep,
    _validate_component,
    _validate_events,
    _write_json,
)
from proteus.safety.taxonomy import (
    MODULE_SAFETY_TAXONOMY_VERSION,
    EvaluationArm,
    HarnessContribution,
    HarnessModule,
    SafetyCaseFamilyDefinition,
    SafetyExposure,
    SafetyKind,
    SafetyStatus,
    TransitionDirection,
)


@dataclass(frozen=True)
class HarnessSafetyResult:
    taxonomy_version: str
    suite: str
    suite_version: str
    family_id: str
    run_id: str
    adapter: str
    arm: str
    seed: int
    episode: int
    primary_module: HarnessModule
    supporting_modules: tuple[HarnessModule, ...]
    safety_kind: SafetyKind
    behavior_status: SafetyStatus
    module_status: SafetyStatus
    exposure: SafetyExposure
    contribution: HarnessContribution
    module_causal_status: CausalStatus
    behavior_failures: tuple[str, ...]
    module_failures: tuple[str, ...]
    evidence: tuple[HarnessSafetyEvidence, ...]
    error: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class SafetyTransitionResult:
    family_id: str
    run_id: str
    from_episode: int
    to_episode: int
    direction: TransitionDirection
    changes: Mapping[str, Mapping[str, object]]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class HarnessSafetyRunSummary:
    evaluation_id: str
    evaluation_root: Path
    results_path: Path
    transitions_path: Path
    summary_path: Path
    total_results: int
    total_transitions: int


def _append_jsonl(sink, value: object) -> None:
    sink.write(json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str))
    sink.write("\n")
    sink.flush()


def _error_result(
    *,
    suite: HarnessSafetyCaseSuite,
    definition: SafetyCaseFamilyDefinition,
    run_id: str,
    adapter: HarnessAdapter,
    arm: str,
    seed: int,
    episode: int,
    status: SafetyStatus,
    error: str,
) -> HarnessSafetyResult:
    return HarnessSafetyResult(
        taxonomy_version=MODULE_SAFETY_TAXONOMY_VERSION,
        suite=suite.name,
        suite_version=suite.version,
        family_id=definition.family_id,
        run_id=run_id,
        adapter=adapter.name,
        arm=arm,
        seed=seed,
        episode=episode,
        primary_module=definition.primary_module,
        supporting_modules=definition.supporting_modules,
        safety_kind=definition.safety_kind,
        behavior_status=status,
        module_status=status,
        exposure=SafetyExposure.UNKNOWN,
        contribution=HarnessContribution.NOT_EVALUATED,
        module_causal_status=CausalStatus.NOT_EVALUATED,
        behavior_failures=(),
        module_failures=(),
        evidence=(),
        error=error,
    )


def _result(
    *,
    suite: HarnessSafetyCaseSuite,
    definition: SafetyCaseFamilyDefinition,
    assessment: FamilyAssessment,
    run_id: str,
    adapter: HarnessAdapter,
    arm: str,
    seed: int,
    episode: int,
) -> HarnessSafetyResult:
    return HarnessSafetyResult(
        taxonomy_version=MODULE_SAFETY_TAXONOMY_VERSION,
        suite=suite.name,
        suite_version=suite.version,
        family_id=definition.family_id,
        run_id=run_id,
        adapter=adapter.name,
        arm=arm,
        seed=seed,
        episode=episode,
        primary_module=definition.primary_module,
        supporting_modules=definition.supporting_modules,
        safety_kind=definition.safety_kind,
        behavior_status=assessment.behavior_status,
        module_status=assessment.module_status,
        exposure=assessment.exposure,
        contribution=assessment.contribution,
        module_causal_status=assessment.module_causal_status,
        behavior_failures=assessment.behavior_failures,
        module_failures=assessment.module_failures,
        evidence=assessment.evidence,
    )


def _not_exposed_evidence(
    definition: SafetyCaseFamilyDefinition,
    missing: Sequence[HarnessModule],
) -> tuple[HarnessSafetyEvidence, ...]:
    reason = "required harness modules are not exposed: " + ", ".join(
        module.value for module in missing
    )
    return tuple(
        HarnessSafetyEvidence(
            arm=arm,
            evaluable=False,
            exposure=SafetyExposure.NOT_EXPOSED,
            reason=reason,
        )
        for arm in definition.evaluation_arms
    )


def _collect_family(
    *,
    definition: SafetyCaseFamilyDefinition,
    provider: HarnessSafetyEvidenceProvider,
    context: HarnessSafetyContext,
) -> tuple[HarnessSafetyEvidence, ...]:
    missing = [
        module
        for module in (definition.primary_module, *definition.supporting_modules)
        if context.profile.binding_for(module) is None
    ]
    if missing:
        return _not_exposed_evidence(definition, missing)

    collected: list[HarnessSafetyEvidence] = []
    for arm in definition.evaluation_arms:
        with tempfile.TemporaryDirectory(prefix="proteus-harness-safety-arm-") as temporary:
            arm_snapshot = Path(temporary) / "snapshot"
            shutil.copytree(context.snapshot_root, arm_snapshot)
            arm_context = replace(context, snapshot_root=arm_snapshot)
            evidence = provider.collect(definition, arm, arm_context)
        if evidence.arm is not arm:
            raise ValueError(
                f"provider returned {evidence.arm.value} evidence for {arm.value} request"
            )
        collected.append(evidence)
    return tuple(collected)


def _full_evidence(result: HarnessSafetyResult) -> HarnessSafetyEvidence | None:
    return next(
        (item for item in result.evidence if item.arm is EvaluationArm.FULL_HARNESS),
        None,
    )


def _ranked_components(result: HarnessSafetyResult) -> dict[str, tuple[object, int | None]]:
    full = _full_evidence(result)
    responsibility = full.responsibility if full is not None else None
    status_rank = {SafetyStatus.PASS: 0, SafetyStatus.FAIL: 1}
    contribution_rank = {
        HarnessContribution.PROTECTIVE_STEERING: 0,
        HarnessContribution.PROTECTIVE_CONTAINMENT: 0,
        HarnessContribution.NEUTRAL: 1,
        HarnessContribution.HARMFUL_INDUCTION: 2,
        HarnessContribution.HARMFUL_AMPLIFICATION: 2,
    }
    model_behavior_rank = {
        ModelBehavior.REFUSED: 0,
        ModelBehavior.SAFE_RESPONSE: 0,
        ModelBehavior.UNSAFE_RESPONSE: 1,
        ModelBehavior.UNSAFE_ACTION_PROPOSAL: 1,
    }

    def bad_boolean(value: bool | None) -> int | None:
        return None if value is None else int(value)

    def good_boolean(value: bool | None) -> int | None:
        return None if value is None else int(not value)

    return {
        "model_behavior": (
            None if responsibility is None else responsibility.model_behavior.value,
            None
            if responsibility is None
            else model_behavior_rank.get(responsibility.model_behavior),
        ),
        "behavior_status": (
            result.behavior_status.value,
            status_rank.get(result.behavior_status),
        ),
        "module_status": (
            result.module_status.value,
            status_rank.get(result.module_status),
        ),
        "harness_contribution": (
            result.contribution.value,
            contribution_rank.get(result.contribution),
        ),
        "harmful_effect_committed": (
            None if responsibility is None else responsibility.harmful_effect_committed,
            None
            if responsibility is None
            else bad_boolean(responsibility.harmful_effect_committed),
        ),
        "persisted_or_propagated": (
            None if responsibility is None else responsibility.persisted_or_propagated,
            None
            if responsibility is None
            else bad_boolean(responsibility.persisted_or_propagated),
        ),
        "contained": (
            None if responsibility is None else responsibility.contained,
            None if responsibility is None else good_boolean(responsibility.contained),
        ),
        "recovered": (
            None if responsibility is None else responsibility.recovered,
            None if responsibility is None else good_boolean(responsibility.recovered),
        ),
        "utility_preserved": (
            None if responsibility is None else responsibility.utility_preserved,
            None
            if responsibility is None
            else good_boolean(responsibility.utility_preserved),
        ),
    }


def compare_adjacent_results(
    before: HarnessSafetyResult,
    after: HarnessSafetyResult,
) -> SafetyTransitionResult:
    before_components = _ranked_components(before)
    after_components = _ranked_components(after)
    changes: dict[str, Mapping[str, object]] = {}
    improved = False
    regressed = False
    comparable = False
    for name, (before_value, before_rank) in before_components.items():
        after_value, after_rank = after_components[name]
        if before_value != after_value:
            changes[name] = {"from": before_value, "to": after_value}
        if before_rank is None or after_rank is None:
            continue
        comparable = True
        improved = improved or after_rank < before_rank
        regressed = regressed or after_rank > before_rank
    if improved and regressed:
        direction = TransitionDirection.MIXED
    elif improved:
        direction = TransitionDirection.IMPROVED
    elif regressed:
        direction = TransitionDirection.REGRESSED
    elif comparable:
        direction = TransitionDirection.UNCHANGED
    else:
        direction = TransitionDirection.NOT_EVALUATED
    return SafetyTransitionResult(
        family_id=after.family_id,
        run_id=after.run_id,
        from_episode=before.episode,
        to_episode=after.episode,
        direction=direction,
        changes=changes,
    )


def _transitions(results: Sequence[HarnessSafetyResult]) -> list[SafetyTransitionResult]:
    by_identity: dict[tuple[str, str], list[HarnessSafetyResult]] = {}
    for result in results:
        by_identity.setdefault((result.run_id, result.family_id), []).append(result)
    transitions: list[SafetyTransitionResult] = []
    for family_results in by_identity.values():
        ordered = sorted(family_results, key=lambda result: result.episode)
        transitions.extend(
            compare_adjacent_results(before, after)
            for before, after in pairwise(ordered)
            if after.episode == before.episode + 1
        )
    return transitions


def _counts(values: Sequence[str]) -> dict[str, int]:
    return dict(sorted(Counter(values).items()))


def _publish_index(safety_root: Path, entry: dict[str, object]) -> None:
    index_path = safety_root / "index.json"
    if index_path.exists():
        index = json.loads(index_path.read_text(encoding="utf-8"))
        if not isinstance(index.get("evaluations"), list):
            raise ValueError("safety index needs an evaluations list")
    else:
        index = {"evaluations": []}
    index["evaluations"].append(entry)
    temporary = safety_root / "index.json.tmp"
    _write_json(temporary, index)
    temporary.replace(index_path)


def run_harness_safety(
    sweep_root: Path,
    adapter: HarnessAdapter,
    suite: HarnessSafetyCaseSuite,
    *,
    evaluation_id: str = "",
) -> HarnessSafetyRunSummary:
    """Evaluate every materialized snapshot without changing the source trajectory."""
    sweep_root = Path(sweep_root)
    manifest, records = _load_sweep(sweep_root)
    planned = _planned_runs(manifest)
    _validate_completed_sweep(manifest, records, planned)
    if not isinstance(adapter, HarnessSafetyAdapter):
        raise TypeError("harness adapter does not declare a harness safety profile")
    profile = adapter.harness_safety_profile()
    profile.validate_surfaces(tuple(adapter.surfaces()))
    if not suite.name.strip() or not suite.version.strip():
        raise TypeError("harness safety suite needs non-empty name and version")
    definitions = tuple(suite.definitions())
    if not definitions:
        raise ValueError("harness safety suite has no case families")
    provider = suite.provider()
    if not isinstance(provider, HarnessSafetyEvidenceProvider):
        raise TypeError("harness safety suite returned an invalid evidence provider")
    for definition in definitions:
        _validate_component(definition.family_id, "family ID")
    if len({definition.family_id for definition in definitions}) != len(definitions):
        raise ValueError("harness safety suite has duplicate family ID")

    evaluation_id = _validate_component(evaluation_id or suite.name, "evaluation ID")
    safety_root = sweep_root / "safety"
    evaluation_root = safety_root / evaluation_id
    if evaluation_root.exists():
        raise FileExistsError(f"safety evaluation already exists: {evaluation_id}")
    evaluation_root.mkdir(parents=True)
    (evaluation_root / "evidence").mkdir()
    _write_json(
        evaluation_root / "manifest.json",
        {
            "evaluation_id": evaluation_id,
            "suite": suite.name,
            "suite_version": suite.version,
            "taxonomy_version": MODULE_SAFETY_TAXONOMY_VERSION,
            "adapter": adapter.name,
            "source_sweep": str(sweep_root.resolve()),
            "created_at": _utc_now(),
            "run_ids": list(planned.values()),
        },
    )

    results: list[HarnessSafetyResult] = []
    results_path = evaluation_root / "results.jsonl"
    with results_path.open("a", encoding="utf-8") as sink:
        for record in records:
            run_arm = str(record["arm"])
            seed = int(record["seed"])
            run_id = planned[(run_arm, seed)]
            run_root = sweep_root / "runs" / run_id
            work_tree = run_root / "harness"
            for episode in range(int(record["episodes_complete"]) + 1):
                commit = snapshot.commit_for_episode(work_tree, episode)
                if commit is None:
                    for definition in definitions:
                        result = _error_result(
                            suite=suite,
                            definition=definition,
                            run_id=run_id,
                            adapter=adapter,
                            arm=run_arm,
                            seed=seed,
                            episode=episode,
                            status=SafetyStatus.INVALID,
                            error=f"episode {episode} snapshot is missing",
                        )
                        results.append(result)
                        _append_jsonl(sink, result.to_dict())
                    continue
                events = () if episode == 0 else tuple(adapter.read_trace(run_root, episode))
                _validate_events(events)
                with tempfile.TemporaryDirectory(prefix="proteus-harness-safety-") as temporary:
                    materialized = Path(temporary) / "snapshot"
                    snapshot.materialize(work_tree, commit, materialized)
                    for definition in definitions:
                        evidence_dir = (
                            evaluation_root
                            / "evidence"
                            / run_id
                            / f"episode-{episode}"
                            / definition.family_id
                        )
                        evidence_dir.mkdir(parents=True, exist_ok=True)
                        context = HarnessSafetyContext(
                            run_id=run_id,
                            adapter_name=adapter.name,
                            arm=run_arm,
                            seed=seed,
                            episode=episode,
                            snapshot_root=materialized,
                            evaluation_root=evaluation_root,
                            evidence_dir=evidence_dir,
                            profile=profile,
                            events=events,
                        )
                        try:
                            evidence = _collect_family(
                                definition=definition,
                                provider=provider,
                                context=context,
                            )
                            assessment = evaluate_family(definition, evidence)
                            result = _result(
                                suite=suite,
                                definition=definition,
                                assessment=assessment,
                                run_id=run_id,
                                adapter=adapter,
                                arm=run_arm,
                                seed=seed,
                                episode=episode,
                            )
                        except Exception as exc:  # noqa: BLE001 - one family must not stop the run
                            result = _error_result(
                                suite=suite,
                                definition=definition,
                                run_id=run_id,
                                adapter=adapter,
                                arm=run_arm,
                                seed=seed,
                                episode=episode,
                                status=SafetyStatus.ERROR,
                                error=f"{type(exc).__name__}: {exc}",
                            )
                        results.append(result)
                        _append_jsonl(sink, result.to_dict())

    transitions = _transitions(results)
    transitions_path = evaluation_root / "transitions.jsonl"
    with transitions_path.open("a", encoding="utf-8") as sink:
        for transition in transitions:
            _append_jsonl(sink, transition.to_dict())
    summary_path = evaluation_root / "summary.json"
    summary = {
        "total_results": len(results),
        "total_transitions": len(transitions),
        "behavior_status_counts": _counts(
            [result.behavior_status.value for result in results]
        ),
        "module_status_counts": _counts(
            [result.module_status.value for result in results]
        ),
        "module_counts": _counts([result.primary_module.value for result in results]),
        "contribution_counts": _counts(
            [result.contribution.value for result in results]
        ),
        "transition_counts": _counts(
            [transition.direction.value for transition in transitions]
        ),
    }
    _write_json(summary_path, summary)
    safety_root.mkdir(exist_ok=True)
    _publish_index(
        safety_root,
        {
            "id": evaluation_id,
            "suite": suite.name,
            "version": suite.version,
            "summary": f"{evaluation_id}/summary.json",
            "results": f"{evaluation_id}/results.jsonl",
            "transitions": f"{evaluation_id}/transitions.jsonl",
        },
    )
    return HarnessSafetyRunSummary(
        evaluation_id=evaluation_id,
        evaluation_root=evaluation_root,
        results_path=results_path,
        transitions_path=transitions_path,
        summary_path=summary_path,
        total_results=len(results),
        total_transitions=len(transitions),
    )
