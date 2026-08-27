"""Candidate-gated activation keeps current-main's transaction semantics."""

from __future__ import annotations

from pathlib import Path

from proteus.adapters.minimal import MinimalHarness, mock_policy
from proteus.bench.task import BenchTask
from proteus.core import EpisodeResult, Goal, GoalConfig, Visibility, review, snapshot
from proteus.core.activation import CandidateGateResult
from proteus.core.episode import RunConfig, pending_candidate_path, run
from proteus.core.goal import EvalResult
from proteus.core.snapshot import SnapshotRef, SnapshotRole
from proteus.safety.indicators import EvolutionSafetyIndicators, FamilyIndicatorProjection
from proteus.safety.permission_cases import PERMISSION_CASE_SPECS
from proteus.safety.permission_evidence import (
    PermissionCapabilityState,
    PermissionCaseCapability,
    PermissionCaseComparison,
    PermissionComparisonStatus,
    PermissionEvidenceValidity,
)
from proteus.safety.permission_executor import reduce_permission_family
from proteus.safety.policy import evaluate_safety_policy


class RecordingHarness(MinimalHarness):
    def __init__(self) -> None:
        super().__init__(policy=mock_policy)
        self.prompts: dict[int, dict[str, str]] = {}

    def run_episode(self, spec):
        self.prompts[spec.episode] = dict(spec.phase_prompts)
        return super().run_episode(spec)


class ScriptedGate:
    def __init__(self, outcomes: list[CandidateGateResult]) -> None:
        self.outcomes = outcomes
        self.contexts = []

    def evaluate(self, context):
        self.contexts.append(context)
        return self.outcomes[context.episode - 1]


def _permission_comparison(case_spec, status):
    supported = PermissionCaseCapability(
        PermissionCapabilityState.SUPPORTED, "fixture-native-policy", ""
    )
    active = SnapshotRef("run-candidate-test", 0, SnapshotRole.ACTIVE)
    candidate = SnapshotRef("run-candidate-test", 1, SnapshotRole.CANDIDATE)
    return PermissionCaseComparison(
        family_id="tools_permission_drift",
        family_version="2",
        schema_version="2",
        active_snapshot=active,
        candidate_snapshot=candidate,
        case_id=case_spec.case_id,
        case_spec=case_spec,
        active_capability=supported,
        candidate_capability=supported,
        active_protected=None,
        active_allowed=None,
        candidate_protected=None,
        candidate_allowed=None,
        validity=PermissionEvidenceValidity.VALID,
        comparison_status=status,
        reasons=(),
        evidence_refs=(),
    )


def run_one_candidate(
    tmp_path: Path,
    *,
    task_selected: bool,
    permission_cases: tuple[PermissionComparisonStatus, ...],
):
    family = reduce_permission_family(
        cases=tuple(
            _permission_comparison(case_spec, status)
            for case_spec, status in zip(
                PERMISSION_CASE_SPECS, permission_cases, strict=True
            )
        )
    )
    profile = EvolutionSafetyIndicators(
        (
            FamilyIndicatorProjection(
                family_id=family.family_id,
                family_version=family.family_version,
                terminal_status=family.terminal_status,
                active_status=None,
                candidate_status=None,
                comparison_status=family.comparison_status,
                evidence_validity=family.validity,
                active_components=None,
                candidate_components=None,
                blockers=family.blockers,
            ),
        )
    )
    decision = evaluate_safety_policy(profile)
    gate = ScriptedGate(
        [
            CandidateGateResult(
                decision.allowed, decision.status.value, "gates/permission"
            )
        ]
    )

    def evaluator(_trace, _context):
        return EvalResult(
            name="task",
            score=1.0,
            error=not task_selected,
        )

    return run(
        _cfg(
            tmp_path,
            gate=gate,
            goal=GoalConfig.single(Goal("task", evaluator=evaluator)),
            episodes=1,
        )
    )


def _cfg(tmp_path, *, gate, goal=None, task=None, episodes=2, grader_sandbox=None):
    return RunConfig(
        name="candidate-test",
        run_id="run-candidate-test",
        adapter=RecordingHarness(),
        disposition=review("notes"),
        goal=goal or GoalConfig.no_goal(),
        root=tmp_path / "run",
        model="mock",
        episodes=episodes,
        seed=0,
        candidate_gate=gate,
        task=task,
        grader_sandbox=grader_sandbox,
    )


def _files(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_candidate_requires_task_selection_and_six_valid_permission_passes(
    tmp_path: Path,
) -> None:
    result = run_one_candidate(
        tmp_path,
        task_selected=True,
        permission_cases=(PermissionComparisonStatus.PASS,) * 5
        + (PermissionComparisonStatus.NOT_EVALUATED,),
    )
    assert result.eval_history[0]["accepted"] is True
    assert result.eval_history[0]["activated"] is True
    assert result.eval_history[0]["safety_status"] == "not_evaluated"


def test_gate_uses_frozen_candidate_and_complete_private_task_view(tmp_path):
    gate = ScriptedGate([CandidateGateResult(True, "pass", "gates/one")])
    seen = {}
    grader = object()

    def setup(workspace: Path) -> None:
        (workspace / "task.txt").write_text("seeded", encoding="utf-8")

    task = BenchTask("fixture", "fixture task", setup=setup, grade=lambda *_: None)

    def evaluator(_trace, context):
        seen["harness"] = Path(context.harness_root)
        seen["active"] = Path(context.active_harness_root)
        seen["task"] = Path(context.task_root)
        seen["grader"] = context.grader_sandbox
        seen["task_content"] = (Path(context.task_root) / "task.txt").read_text(encoding="utf-8")
        (Path(context.harness_root) / "task-only-mutation.txt").write_text("mutated")
        return EvalResult(name="task", score=1.0)

    result = run(_cfg(
        tmp_path,
        gate=gate,
        goal=GoalConfig.single(Goal("task", evaluator=evaluator)),
        task=task,
        episodes=1,
        grader_sandbox=grader,
    ))

    harness = Path(result.root) / "harness"
    assert seen["harness"] != harness
    assert seen["active"] != harness
    assert seen["task"] == seen["harness"].parent / "task"
    assert seen["task_content"] == "seeded"
    assert seen["grader"] is grader
    assert gate.contexts[0].candidate_root != seen["harness"]
    assert not (gate.contexts[0].candidate_root / "task-only-mutation.txt").exists()
    assert result.eval_history[0]["activated"] is True


def test_nonpassing_gate_is_audit_only_and_still_activates_task_selected_tree(tmp_path):
    gate = ScriptedGate([
        CandidateGateResult(True, "pass", "gates/one"),
        CandidateGateResult(False, "fail", "gates/two"),
    ])
    result = run(_cfg(tmp_path, gate=gate))
    harness = Path(result.root) / "harness"
    active_one = snapshot.commit_for_episode(harness, 1)
    active_two = snapshot.commit_for_episode(harness, 2)
    candidate_two = snapshot.candidate_for_episode(harness, 2)

    assert active_one is not None and active_two is not None and candidate_two is not None
    one = tmp_path / "one"
    two = tmp_path / "two"
    candidate = tmp_path / "candidate"
    snapshot.materialize(harness, active_one, one)
    snapshot.materialize(harness, active_two, two)
    snapshot.materialize_candidate(harness, candidate_two, candidate)
    assert _files(two) != _files(one)
    assert _files(two) == _files(candidate)
    assert result.eval_history[1]["safety_status"] == "fail"
    assert result.eval_history[1]["task_selected"] is True
    assert result.eval_history[1]["activated"] is True
    assert [ctx.episode for ctx in gate.contexts] == [1, 2]


def test_audit_gate_does_not_override_accept_reject_score_baseline(tmp_path):
    scores = {1: 1.0, 2: 0.0}

    def evaluator(_trace, context):
        return EvalResult(name="task", score=scores[context.episode])

    gate = ScriptedGate([
        CandidateGateResult(False, "fail", "gates/one"),
        CandidateGateResult(True, "pass", "gates/two"),
    ])
    result = run(_cfg(
        tmp_path,
        gate=gate,
        goal=GoalConfig.single(Goal("task", evaluator=evaluator), selection="accept_reject"),
    ))

    assert [row["task_selected"] for row in result.eval_history] == [True, False]
    assert [row["safety_status"] for row in result.eval_history] == ["fail", "pass"]
    assert [row["activated"] for row in result.eval_history] == [True, False]
    assert [ctx.episode for ctx in gate.contexts] == [1, 2]


def test_task_evaluator_error_prevents_safety_approved_activation(tmp_path):
    def evaluator(_trace, _context):
        raise RuntimeError("grader unavailable")

    result = run(_cfg(
        tmp_path,
        gate=ScriptedGate([CandidateGateResult(True, "pass", "gates/one")]),
        goal=GoalConfig.single(Goal("task", evaluator=evaluator)),
        episodes=1,
    ))

    assert result.eval_history[0]["task_selected"] is False
    assert result.eval_history[0]["activated"] is False


def test_gate_detail_never_enters_agent_feedback(tmp_path):
    sentinel = "SENTINEL-GATE-DETAIL"
    gate = ScriptedGate([CandidateGateResult(False, sentinel, "gates/one")])
    cfg = _cfg(tmp_path, gate=gate, episodes=1)
    result = run(cfg)

    subject_text = "\n".join(
        path.read_text(errors="ignore")
        for path in Path(result.root).rglob("*")
        if path.is_file() and ".snapshot.git" not in path.parts
    )
    assert sentinel not in subject_text
    assert all(sentinel not in prompt for prompts in cfg.adapter.prompts.values() for prompt in prompts.values())
    assert result.eval_history[0]["decision_ref"] == "gates/one"


def test_snapshot_refs_are_logical_and_candidate_materialization_uses_them(tmp_path):
    gate = ScriptedGate([CandidateGateResult(True, "pass", "gates/one")])
    result = run(_cfg(tmp_path, gate=gate, episodes=1))
    harness = Path(result.root) / "harness"
    candidate = snapshot.candidate_for_episode(harness, 1)
    assert candidate is not None
    assert candidate.to_dict() == {
        "run_id": "run-candidate-test", "episode": 1, "role": "candidate",
    }
    destination = tmp_path / "candidate"
    snapshot.materialize_candidate(harness, candidate, destination)
    assert (destination / "STATE.md").is_file()


def test_task_rejection_removes_ignored_candidate_files_from_live_harness(tmp_path):
    class IgnoredCandidateHarness(RecordingHarness):
        def run_episode(self, spec):
            result = super().run_episode(spec)
            (spec.root / "harness" / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
            (spec.root / "harness" / "ignored.txt").write_text("candidate", encoding="utf-8")
            return result

    def evaluator(_trace, _context):
        raise RuntimeError("grader unavailable")

    cfg = _cfg(
        tmp_path,
        gate=ScriptedGate([CandidateGateResult(True, "pass", "gates/one")]),
        goal=GoalConfig.single(Goal("task", evaluator=evaluator)),
        episodes=1,
    )
    cfg.adapter = IgnoredCandidateHarness()
    result = run(cfg)
    harness = Path(result.root) / "harness"
    candidate = snapshot.candidate_for_episode(harness, 1)
    assert candidate is not None
    preserved = tmp_path / "preserved"
    snapshot.materialize_candidate(harness, candidate, preserved)
    assert (preserved / "ignored.txt").read_text(encoding="utf-8") == "candidate"
    assert not (harness / "ignored.txt").exists()
    assert result.eval_history[0]["task_selected"] is False
    assert result.eval_history[0]["activated"] is False


def test_resume_uses_only_previously_activated_scores_as_its_selection_baseline(tmp_path):
    scores = {1: 1.0, 2: 0.0}

    def evaluator(_trace, context):
        return EvalResult(name="task", score=scores[context.episode])

    goal = GoalConfig.single(Goal("task", evaluator=evaluator), selection="accept_reject")
    first = run(_cfg(
        tmp_path,
        gate=ScriptedGate([CandidateGateResult(False, "fail", "gates/one")]),
        goal=goal,
        episodes=1,
    ))
    resumed_cfg = _cfg(
        tmp_path,
        gate=ScriptedGate([
            CandidateGateResult(False, "fail", "unused"),
            CandidateGateResult(True, "pass", "gates/two"),
        ]),
        goal=goal,
        episodes=2,
    )
    resumed = run(resumed_cfg, start=first.episodes_complete, resume=True)

    assert [row["activated"] for row in resumed.eval_history] == [True, False]
    assert resumed.eval_history[0]["safety_status"] == "fail"
    assert resumed.eval_history[1]["task_selected"] is False


def test_safety_rejection_never_marks_observe_feedback_as_not_kept(tmp_path):
    sentinel = "SENTINEL-SAFETY-ONLY-REJECTION"

    def evaluator(_trace, _context):
        return EvalResult(name="task", score=1.0, detail="task feedback")

    goal = GoalConfig.single(
        Goal("task", evaluator=evaluator, visibility=Visibility.OBSERVE)
    )
    cfg = _cfg(
        tmp_path,
        gate=ScriptedGate([
            CandidateGateResult(False, sentinel, "gates/one"),
            CandidateGateResult(True, "pass", "gates/two"),
        ]),
        goal=goal,
    )
    result = run(cfg)

    observe = cfg.adapter.prompts[2]["observe"]
    assert "Feedback on your last episode" in observe
    assert "not kept" not in observe
    assert sentinel not in observe
    assert result.eval_history[0]["task_selected"] is True
    assert result.eval_history[0]["safety_status"] == sentinel
    assert result.eval_history[0]["activated"] is True


def test_resume_after_safety_rejection_never_marks_observe_feedback_as_not_kept(tmp_path):
    sentinel = "SENTINEL-RESUMED-SAFETY-REJECTION"

    def evaluator(_trace, _context):
        return EvalResult(name="task", score=1.0, detail="task feedback")

    goal = GoalConfig.single(
        Goal("task", evaluator=evaluator, visibility=Visibility.OBSERVE)
    )
    first = run(_cfg(
        tmp_path,
        gate=ScriptedGate([CandidateGateResult(False, sentinel, "gates/one")]),
        goal=goal,
        episodes=1,
    ))
    resumed_cfg = _cfg(
        tmp_path,
        gate=ScriptedGate([
            CandidateGateResult(False, "unused", "unused"),
            CandidateGateResult(True, "pass", "gates/two"),
        ]),
        goal=goal,
        episodes=2,
    )
    run(resumed_cfg, start=first.episodes_complete, resume=True)

    observe = resumed_cfg.adapter.prompts[2]["observe"]
    assert "Feedback on your last episode" in observe
    assert "not kept" not in observe
    assert sentinel not in observe


def test_staged_viability_repair_keeps_active_runtime_then_activates_through_gate(tmp_path):
    class StagedHarness(MinimalHarness):
        staged_activation = True

        def __init__(self) -> None:
            super().__init__(policy=mock_policy)
            self.active_observations = []

        def run_episode(self, spec):
            active = Path(spec.active_root)
            candidate = spec.root / "harness"
            self.active_observations.append({
                "episode": spec.episode,
                "active_broken": (active / "BROKEN").exists(),
                "candidate_broken": (candidate / "BROKEN").exists(),
            })
            if spec.episode == 1:
                (candidate / "BROKEN").write_text("does not compile\n", encoding="utf-8")
                assert not (active / "BROKEN").exists()
            else:
                assert (candidate / "BROKEN").read_text(encoding="utf-8") == "does not compile\n"
                assert not (active / "BROKEN").exists()
                (candidate / "BROKEN").unlink()
                (candidate / "repaired.txt").write_text("healthy\n", encoding="utf-8")
            return EpisodeResult(episode=spec.episode, ok=True)

        def validate_candidate(self, harness_root):
            return "compile failed" if (Path(harness_root) / "BROKEN").exists() else ""

    adapter = StagedHarness()
    gate = ScriptedGate([
        CandidateGateResult(False, "unused", "unused"),
        CandidateGateResult(True, "pass", "gates/two"),
    ])
    root = tmp_path / "staged"
    result = run(RunConfig(
        name="staged", run_id="run-staged", adapter=adapter, disposition=review("notes"),
        goal=GoalConfig(), root=root, model="mock", episodes=2, candidate_gate=gate,
    ))

    assert adapter.active_observations == [
        {"episode": 1, "active_broken": False, "candidate_broken": False},
        {"episode": 2, "active_broken": False, "candidate_broken": True},
    ]
    assert [context.episode for context in gate.contexts] == [2]
    assert result.eval_history[0]["failure_kind"] == "viability"
    assert result.eval_history[1]["activated"] is True
    assert (root / "harness" / "repaired.txt").read_text(encoding="utf-8") == "healthy\n"
    assert not pending_candidate_path(root).exists()
