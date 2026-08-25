"""Candidate-gated activation keeps current-main's transaction semantics."""

from __future__ import annotations

from pathlib import Path

from proteus.adapters.minimal import MinimalHarness, mock_policy
from proteus.bench.task import BenchTask
from proteus.core import Goal, GoalConfig, review, snapshot
from proteus.core.activation import CandidateGateResult
from proteus.core.episode import RunConfig, run
from proteus.core.goal import EvalResult


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


def test_nonpassing_gate_preserves_candidate_and_restores_active_tree(tmp_path):
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
    rejected = tmp_path / "rejected"
    snapshot.materialize(harness, active_one, one)
    snapshot.materialize(harness, active_two, two)
    snapshot.materialize_candidate(harness, candidate_two, rejected)
    assert _files(two) == _files(one)
    assert _files(rejected) != _files(one)
    assert result.eval_history[1]["safety_status"] == "fail"
    assert result.eval_history[1]["activated"] is False


def test_rejected_gate_does_not_advance_accept_reject_score_baseline(tmp_path):
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

    assert [row["task_selected"] for row in result.eval_history] == [True, True]
    assert [row["activated"] for row in result.eval_history] == [False, True]


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


def test_gate_rejection_removes_ignored_candidate_files_from_live_harness(tmp_path):
    class IgnoredCandidateHarness(RecordingHarness):
        def run_episode(self, spec):
            result = super().run_episode(spec)
            (spec.root / "harness" / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
            (spec.root / "harness" / "ignored.txt").write_text("candidate", encoding="utf-8")
            return result

    cfg = _cfg(
        tmp_path,
        gate=ScriptedGate([CandidateGateResult(False, "fail", "gates/one")]),
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

    assert [row["activated"] for row in resumed.eval_history] == [False, True]
    assert resumed.eval_history[1]["task_selected"] is True
