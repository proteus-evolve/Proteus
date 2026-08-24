"""Goal/evaluator paths: visibility routing, multi-goal, accept/reject selection."""

import subprocess
from pathlib import Path

from proteus.adapters.minimal import MinimalHarness, mock_policy
from proteus.core import NEUTRAL, Goal, GoalConfig, Visibility, snapshot
from proteus.core.episode import RunConfig, run
from proteus.core.evaluators import surface_units, tool_calls
from proteus.core.goal import EvalResult


class RecordingHarness(MinimalHarness):
    """Minimal harness that also records each episode's phase prompts."""

    def __init__(self):
        super().__init__(policy=mock_policy)
        self.prompts: dict[int, dict[str, str]] = {}

    def run_episode(self, spec):
        self.prompts[spec.episode] = dict(spec.phase_prompts)
        return super().run_episode(spec)


def _cfg(tmp_path, adapter, goal, episodes=3, disposition=NEUTRAL):
    return RunConfig(name="t", run_id="run-goals", adapter=adapter, disposition=disposition, goal=goal,
                     root=tmp_path / "run", model="mock", episodes=episodes, seed=0)


def test_observe_feedback_reaches_next_observe_prompt(tmp_path):
    adapter = RecordingHarness()
    goal = GoalConfig.single(Goal("notes", text="Grow your notes.",
                                  evaluator=surface_units("notes", name="notes"),
                                  visibility=Visibility.OBSERVE))
    res = run(_cfg(tmp_path, adapter, goal))
    assert res.episodes_complete == 3
    assert "Feedback on your last episode" in adapter.prompts[2]["observe"]
    assert "Feedback" not in adapter.prompts[1]["observe"]   # nothing before episode 1
    assert "Grow your notes." in adapter.prompts[1]["act"]   # goal text announced in act


def test_hidden_feedback_never_shown(tmp_path):
    adapter = RecordingHarness()
    goal = GoalConfig.single(Goal("notes", text="Grow your notes.",
                                  evaluator=surface_units("notes", name="notes"),
                                  visibility=Visibility.HIDDEN))
    res = run(_cfg(tmp_path, adapter, goal))
    assert res.episodes_complete == 3
    for ep in (2, 3):
        assert "Feedback" not in adapter.prompts[ep]["observe"]
    # scored offline all the same
    assert all(h["results"] for h in res.eval_history)


def test_multi_goal_runs_all_evaluators(tmp_path):
    goal = GoalConfig.multi([
        Goal("notes", text="Grow notes.", evaluator=surface_units("notes", name="notes")),
        Goal("activity", evaluator=tool_calls(name="activity")),
    ])
    res = run(_cfg(tmp_path, MinimalHarness(), goal))
    names = {r["name"] for h in res.eval_history for r in h["results"]}
    assert names == {"notes", "activity"}
    assert "several objectives" not in res.eval_history[0]  # single stated text -> plain


def test_accept_reject_reverts_worse_episode(tmp_path):
    # score drops on episode 2 -> episode 2 must be rejected and its tree reverted
    scores = {1: 1.0, 2: 0.0, 3: 1.0}

    def scripted(trace, ctx):
        return EvalResult(name="s", score=scores[ctx.episode])

    goal = GoalConfig.single(Goal("s", evaluator=scripted), selection="accept_reject")
    # review("notes") guarantees every episode writes, so the rejected candidate tree
    # deterministically differs from the last accepted tree
    from proteus.core import review
    res = run(_cfg(tmp_path, MinimalHarness(), goal, disposition=review("notes")))
    assert res.episodes_complete == 3
    activated = [h["activated"] for h in res.eval_history]
    assert activated == [True, False, True]

    work = Path(res.root) / "harness"
    sha1 = snapshot.commit_for_episode(work, 1)
    sha2 = snapshot.commit_for_episode(work, 2)
    git_dir = work.parent / ".snapshot.git"

    def tree(sha):
        return subprocess.run(["git", "--git-dir", str(git_dir), "rev-parse", f"{sha}^{{tree}}"],
                              capture_output=True, text=True, check=True).stdout.strip()

    assert sha2 is not None                  # mapping stays gapless
    assert tree(sha2) == tree(sha1)          # rejected episode's tree == last accepted

    # non-destructive: the rejected candidate tree is preserved in history
    log = subprocess.run(["git", "--git-dir", str(git_dir), "log", "--format=%H %s"],
                         capture_output=True, text=True, check=True).stdout
    cand = next((l.split()[0] for l in log.splitlines()
                 if "candidate 2:" in l), None)
    assert cand is not None
    assert tree(cand) != tree(sha1)          # the discarded work is still inspectable
