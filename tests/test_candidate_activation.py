"""Candidate snapshots are evaluated off-tree and activated only by conjunction."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from proteus.adapters.minimal import MinimalHarness, mock_policy
from proteus.core import Goal, GoalConfig, review, snapshot
from proteus.core.activation import CandidateGateResult
from proteus.core.episode import RunConfig, run
from proteus.core.goal import EvalResult


class RecordingHarness(MinimalHarness):
    """A real minimal harness that retains the prompts it was given."""

    def __init__(self) -> None:
        super().__init__(policy=mock_policy)
        self.prompts: dict[int, dict[str, str]] = {}

    def run_episode(self, spec):
        self.prompts[spec.episode] = dict(spec.phase_prompts)
        return super().run_episode(spec)


class ScriptedGate:
    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.contexts = []
        self.candidate_has_notes = []

    def evaluate(self, context):
        self.contexts.append(context)
        self.candidate_has_notes.append((context.candidate_root / "notes").is_dir())
        outcome = self.outcomes[context.episode - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class CandidateMutationGate:
    def __init__(self) -> None:
        self.saw_task_mutation = False

    def evaluate(self, context):
        self.saw_task_mutation = (context.candidate_root / "task-evaluator-mutation.txt").exists()
        return CandidateGateResult(True, "pass", "gates/one")


def _cfg(tmp_path, *, gate, goal=None, episodes=2, progress_path=None):
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
        progress_path=progress_path,
    )


def _files(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_freezes_candidate_before_task_and_gate_evaluation(tmp_path):
    gate = ScriptedGate([CandidateGateResult(True, "pass", "gates/one")])
    seen = []

    work_tree = tmp_path / "run" / "harness"

    def evaluator(_trace, context):
        candidate = snapshot.candidate_for_episode(work_tree, 1)
        seen.append((candidate, Path(context.harness_root)))
        return EvalResult(name="task", score=1.0)

    cfg = _cfg(tmp_path, gate=gate, goal=GoalConfig.single(Goal("task", evaluator=evaluator)), episodes=1)
    run(cfg)

    assert seen[0][0] is not None
    assert seen[0][0].to_dict() == {
        "run_id": "run-candidate-test", "episode": 1, "role": "candidate",
    }
    assert seen[0][1] != work_tree
    assert gate.contexts[0].candidate == seen[0][0]
    assert gate.candidate_has_notes == [True]


def test_pass_activates_the_exact_frozen_candidate_tree(tmp_path):
    gate = ScriptedGate([CandidateGateResult(True, "pass", "gates/one")])
    result = run(_cfg(tmp_path, gate=gate, episodes=1))
    work_tree = Path(result.root) / "harness"
    candidate = snapshot.candidate_for_episode(work_tree, 1)
    active = snapshot.commit_for_episode(work_tree, 1)

    assert candidate is not None
    assert active is not None
    materialized = tmp_path / "candidate"
    snapshot.materialize_candidate(work_tree, candidate, materialized)
    assert any((materialized / "notes").glob("*.md"))
    active_root = tmp_path / "active"
    snapshot.materialize(work_tree, active, active_root)
    assert _files(materialized) == _files(active_root)


@pytest.mark.parametrize(
    "outcome",
    [
        CandidateGateResult(False, "fail", "gates/fail"),
        CandidateGateResult(False, "not_evaluated", "gates/not-evaluated"),
        CandidateGateResult(False, "invalid", "gates/invalid"),
        CandidateGateResult(False, "error", "gates/error"),
        CandidateGateResult(True, "fail", "gates/contradictory-fail"),
        CandidateGateResult(True, "not_evaluated", "gates/contradictory-not-evaluated"),
        CandidateGateResult(True, "invalid", "gates/contradictory-invalid"),
        CandidateGateResult(True, "error", "gates/contradictory-error"),
        RuntimeError("gate crashed"),
    ],
)
def test_nonpassing_gate_restores_previous_active_tree(tmp_path, outcome):
    gate = ScriptedGate([CandidateGateResult(True, "pass", "gates/one"), outcome])
    result = run(_cfg(tmp_path, gate=gate))
    work_tree = Path(result.root) / "harness"
    active_one = snapshot.commit_for_episode(work_tree, 1)
    active_two = snapshot.commit_for_episode(work_tree, 2)

    assert active_one is not None and active_two is not None
    active_one_root = tmp_path / "active-one"
    active_two_root = tmp_path / "active-two"
    snapshot.materialize(work_tree, active_one, active_one_root)
    snapshot.materialize(work_tree, active_two, active_two_root)
    assert _files(active_two_root) == _files(active_one_root)
    rejected = snapshot.candidate_for_episode(work_tree, 2)
    assert rejected is not None
    rejected_root = tmp_path / "rejected"
    snapshot.materialize_candidate(work_tree, rejected, rejected_root)
    assert _files(rejected_root) != _files(active_one_root)


@pytest.mark.parametrize(
    "task_selected,gate_allowed",
    [(False, True), (True, False)],
)
def test_task_selection_and_gate_must_both_allow_activation(tmp_path, task_selected, gate_allowed):
    scores = {1: 1.0, 2: 0.0 if not task_selected else 2.0}

    def evaluator(_trace, context):
        return EvalResult(name="task", score=scores[context.episode])

    gate = ScriptedGate([
        CandidateGateResult(True, "pass", "gates/one"),
        CandidateGateResult(gate_allowed, "pass" if gate_allowed else "fail", "gates/two"),
    ])
    goal = GoalConfig.single(Goal("task", evaluator=evaluator), selection="accept_reject")
    result = run(_cfg(tmp_path, gate=gate, goal=goal))

    assert result.eval_history[1]["task_selected"] is task_selected
    assert result.eval_history[1]["activated"] is False
    assert len(gate.contexts) == 2


def test_safety_gate_receives_an_unmodified_frozen_candidate(tmp_path):
    gate = CandidateMutationGate()

    def evaluator(_trace, context):
        (Path(context.harness_root) / "task-evaluator-mutation.txt").write_text("mutated")
        return EvalResult(name="task", score=1.0)

    result = run(_cfg(
        tmp_path,
        gate=gate,
        goal=GoalConfig.single(Goal("task", evaluator=evaluator)),
        episodes=1,
    ))

    assert result.eval_history[0]["activated"] is True
    assert gate.saw_task_mutation is False


def test_candidates_remain_materializable_and_active_mapping_is_gapless(tmp_path):
    gate = ScriptedGate([
        CandidateGateResult(True, "pass", "gates/one"),
        CandidateGateResult(False, "fail", "gates/two"),
        CandidateGateResult(True, "pass", "gates/three"),
    ])
    result = run(_cfg(tmp_path, gate=gate, episodes=3))
    work_tree = Path(result.root) / "harness"

    for episode in (1, 2, 3):
        assert snapshot.commit_for_episode(work_tree, episode) is not None
        candidate = snapshot.candidate_for_episode(work_tree, episode)
        assert candidate is not None
        destination = tmp_path / f"candidate-{episode}"
        snapshot.materialize_candidate(work_tree, candidate, destination)
        assert (destination / "STATE.md").is_file()


def test_gate_details_stay_out_of_subject_run_and_progress_keeps_only_reference(tmp_path):
    sentinel = "SENTINEL-INDICATOR-FAILURE"
    progress = tmp_path / "controller" / "progress.jsonl"
    gate = ScriptedGate([CandidateGateResult(False, sentinel, "gates/candidate-0001/decision.json")])
    cfg = _cfg(tmp_path, gate=gate, episodes=1, progress_path=progress)
    result = run(cfg)

    subject_text = "\n".join(
        path.read_text(errors="ignore")
        for path in Path(result.root).rglob("*")
        if path.is_file() and ".snapshot.git" not in path.parts
    )
    assert sentinel not in subject_text
    assert all(sentinel not in prompt for prompts in cfg.adapter.prompts.values() for prompt in prompts.values())

    record = json.loads(progress.read_text().strip())
    assert record["task_selected"] is True
    assert record["activated"] is False
    assert record["decision_ref"] == "gates/candidate-0001/decision.json"
    assert "accepted" not in record
    assert sentinel not in json.dumps(result.eval_history)


def test_progress_path_inside_subject_run_is_rejected(tmp_path):
    cfg = _cfg(
        tmp_path,
        gate=ScriptedGate([CandidateGateResult(True, "pass", "gates/one")]),
        episodes=1,
        progress_path=tmp_path / "run" / "controller" / "progress.jsonl",
    )

    with pytest.raises(ValueError, match="outside the subject run"):
        run(cfg)

    assert not cfg.root.exists()
