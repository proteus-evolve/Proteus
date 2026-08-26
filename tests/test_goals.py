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
    return RunConfig(name="t", adapter=adapter, disposition=disposition, goal=goal,
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
    # Each phase is context-fresh, so all four must receive the objective.
    assert all("Grow your notes." in adapter.prompts[1][phase]
               for phase in ("observe", "propose", "act", "reflect"))


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


def test_evaluators_fail_independently_and_reject_nonfinite_scores():
    from proteus.core import EvaluatorSpec
    from proteus.core.goal import GoalContext

    def good(trace, ctx):
        return EvalResult(name="wrong-returned-name", score=0.75, passed=True)

    def broken(trace, ctx):
        raise RuntimeError("grader exploded")

    def nan(trace, ctx):
        return EvalResult(name="nan", score=float("nan"))

    goal = GoalConfig.of(evaluators=(
        EvaluatorSpec(name="good", run=good),
        EvaluatorSpec(name="broken", run=broken),
        EvaluatorSpec(name="nan", run=nan),
    ))
    results = goal.evaluate([], GoalContext("/unused", 1))
    assert [(r.name, r.score) for r in results] == [
        ("good", 0.75), ("broken", 0.0), ("nan", 0.0)]
    assert "RuntimeError" in results[1].detail
    assert "non-finite" in results[2].detail


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
    accepted = [h["accepted"] for h in res.eval_history]
    assert accepted == [True, False, True]

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
    cand = next((ln.split()[0] for ln in log.splitlines()
                 if "candidate 2:" in ln), None)
    assert cand is not None
    assert tree(cand) != tree(sha1)          # the discarded work is still inspectable


# ------------------------------------------------------- decoupled goal / evaluators

def test_goal_text_and_evaluators_are_independent(tmp_path):
    from proteus.core import EvaluatorSpec, GoalConfig, Visibility
    from proteus.core import evaluators as ev
    from proteus.core.goal import GoalContext

    spec = EvaluatorSpec(name="units:notes", run=ev.surface_units("notes"),
                         kind="measurement", visibility=Visibility.OBSERVE)
    cfg = GoalConfig.of(text="Make yourself more robust.", evaluators=(spec,))
    assert cfg.goal_text() == "Make yourself more robust."
    assert not cfg.is_no_goal

    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "a.md").write_text("x\n")
    results = cfg.evaluate([], GoalContext(str(tmp_path), 1))
    assert [r.name for r in results] == ["units:notes"]
    fb = cfg.observe_feedback({r.name: r for r in results})
    assert "units:notes" in fb                      # OBSERVE reaches the agent

    hidden = GoalConfig.of(text="", evaluators=(
        EvaluatorSpec(name="units:notes", run=ev.surface_units("notes"),
                      kind="measurement"),))
    assert hidden.is_no_goal                        # measured but unstated: still no-goal
    r = hidden.evaluate([], GoalContext(str(tmp_path), 1))
    assert hidden.observe_feedback({x.name: x for x in r}) == ""   # HIDDEN never surfaces
    assert hidden.describe() == [
        {"name": "units:notes", "kind": "measurement", "visibility": "hidden"}]


def test_structural_step_reads_movement_between_episodes(tmp_path):
    from proteus.core import NEUTRAL, GoalConfig
    from proteus.core import evaluators as ev
    from proteus.core.episode import RunConfig, run
    from proteus.adapters.minimal import MinimalHarness
    from proteus.core.goal import EvaluatorSpec

    adapter = MinimalHarness()
    step = EvaluatorSpec(name="structural-step",
                         run=ev.structural_step(adapter.surfaces()),
                         kind="measurement")
    cfg = RunConfig(name="t", adapter=adapter, disposition=NEUTRAL,
                    goal=GoalConfig.of(evaluators=(step,)),
                    root=tmp_path / "r", model="mock", episodes=3, seed=2)
    res = run(cfg)
    scores = [r["results"][0]["score"] for r in res.eval_history]
    assert scores[0] == 0.0, "episode 1 has no predecessor"
    assert any(s > 0 for s in scores[1:]), "movement between episodes went unmeasured"


def test_run_sums_numeric_counters(tmp_path):
    from proteus.adapters.minimal import MinimalHarness
    from proteus.core import NEUTRAL, GoalConfig
    from proteus.core.adapter import EpisodeResult
    from proteus.core.episode import RunConfig, run

    class Counting(MinimalHarness):
        def run_episode(self, spec):
            res = super().run_episode(spec)
            return EpisodeResult(episode=res.episode, ok=res.ok, turns=res.turns,
                                 counters={"tokens_in": 100, "tokens_out": 7,
                                           "writes": {"notes": 1}})

    cfg = RunConfig(name="t", adapter=Counting(), disposition=NEUTRAL, goal=GoalConfig(),
                    root=tmp_path / "r", model="mock", episodes=3, seed=1)
    res = run(cfg)
    assert res.counters == {"tokens_in": 300, "tokens_out": 21}, \
        "numeric counters must sum across episodes; nested dicts stay out"


def test_cli_attaches_a_benchmark_evaluator_with_its_task():
    from proteus.cli import _evaluator
    spec, task = _evaluator("local:interval-merge@observe", lambda: None)
    assert spec.kind == "benchmark" and spec.visibility.value == "observe"
    assert task is not None and task.id == "local:interval-merge"
    assert spec.name == task.id
    # measurement evaluators carry no task
    spec2, task2 = _evaluator("tool-calls", lambda: None)
    assert spec2.kind == "measurement" and task2 is None


def test_surface_units_uses_declared_unit_and_supports_file_surfaces(tmp_path):
    from proteus.core.adapter import Surface
    from proteus.core.goal import GoalContext

    instructions = Surface("instructions", "AGENTS.md")
    (tmp_path / "AGENTS.md").write_text("Be useful.\n")
    result = surface_units(instructions)([], GoalContext(str(tmp_path), 1))
    assert result.name == "units:instructions" and result.score == 1

    skills = Surface("skills", "skills", unit="directory")
    (tmp_path / "skills" / "alpha").mkdir(parents=True)
    (tmp_path / "skills" / "alpha" / "SKILL.md").write_text("# Alpha\n")
    (tmp_path / "skills" / "alpha" / "helper.py").write_text("pass\n")
    result = surface_units(skills)([], GoalContext(str(tmp_path), 1))
    assert result.score == 1, "one skill directory is one declared unit"


def test_cli_units_resolves_surface_name_and_legacy_subdir(tmp_path):
    from proteus.cli import _evaluator
    from proteus.core.adapter import Surface
    from proteus.core.goal import GoalContext

    class Declared:
        def surfaces(self):
            return (Surface("instructions", "AGENTS.md"), Surface("loop", "src"))

    (tmp_path / "AGENTS.md").write_text("Be useful.\n")
    named, task = _evaluator("units:instructions", Declared)
    assert task is None and named.name == "units:instructions"
    assert named.run([], GoalContext(str(tmp_path), 1)).score == 1

    legacy, _ = _evaluator("units:src", Declared)
    assert legacy.name == "units:loop", "subdir input must canonicalize to surface name"


def test_cli_run_returns_failure_when_an_episode_fails(tmp_path):
    from unittest.mock import patch

    from proteus import cli
    from proteus.core.adapter import EpisodeResult

    class FailingHarness(MinimalHarness):
        def run_episode(self, spec):
            return EpisodeResult(episode=spec.episode, ok=False, error="boom")

    with patch.object(cli, "_harness_factory", return_value=FailingHarness):
        rc = cli.main(["run", "--harness", "minimal", "--arm", "neutral",
                       "--seeds", "1", "--episodes", "1",
                       "--out", str(tmp_path / "failed")])
    assert rc == 1


def test_cli_manifest_does_not_publish_contains_evaluator_needle(tmp_path):
    import json

    from proteus import cli

    root = tmp_path / "private-evaluator"
    rc = cli.main([
        "run", "--harness", "minimal", "--arm", "neutral", "--seeds", "1",
        "--episodes", "1", "--evaluator", "contains:AGENTS.md:private-needle",
        "--out", str(root),
    ])
    assert rc == 0
    manifest_text = (root / "manifest.json").read_text()
    assert "private-needle" not in manifest_text
    metadata = json.loads(manifest_text)["condition"]["metadata"]
    assert len(metadata["evaluator_specs"][0]) == 64


def test_cli_rejects_invalid_turn_reservations_without_traceback(tmp_path):
    from proteus import cli

    try:
        cli.main(["run", "--harness", "minimal", "--arm", "neutral",
                  "--max-turns", "7", "--min-turns-per-phase", "2",
                  "--out", str(tmp_path / "bad")])
    except SystemExit as exc:
        assert "use --max-turns >= 8" in str(exc)
    else:
        raise AssertionError("invalid reservation was accepted")


def test_cli_accepts_and_records_an_explicit_phase_budget(tmp_path):
    import json
    from proteus import cli

    root = tmp_path / "phase-budget"
    rc = cli.main([
        "run", "--harness", "minimal", "--arm", "neutral", "--seeds", "1",
        "--episodes", "1", "--max-turns", "8", "--hard-max-turns", "12",
        "--phase-turns", "observe=2,propose=2,act=2,reflect=2",
        "--announce-budget", "--out", str(root),
    ])
    assert rc == 0
    manifest = json.loads((root / "manifest.json").read_text())
    assert manifest["budget"]["hard_limit"] == 12


def test_checkpoint_budget_requires_announcement(tmp_path):
    from proteus import cli

    try:
        cli.main([
            "run", "--harness", "dsh", "--arm", "neutral", "--seeds", "1",
            "--episodes", "1", "--max-turns", "8",
            "--phase-turns", "observe=2,propose=2,act=2,reflect=2",
            "--checkpoint-turns", "1", "--out", str(tmp_path / "bad-checkpoint"),
        ])
    except SystemExit as exc:
        assert "requires --announce-budget" in str(exc)
    else:
        raise AssertionError("a hidden checkpoint reserve was accepted")
