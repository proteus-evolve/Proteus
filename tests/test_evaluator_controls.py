"""Public evaluator policies, sparse feedback, and exact resume selection semantics."""
from dataclasses import replace
import json

import pytest

from proteus.core import EvalResult, EvaluatorSpec, GoalConfig, Visibility
from proteus.core.episode import eval_history_path, private_record_dir, run
from proteus.core.feedback import EvaluatorFeedback
from proteus.core.goal import GoalContext
from test_goals import RecordingHarness, _cfg


def fixed(score=1.0, detail=""):
    return lambda trace, ctx: EvalResult("returned-name", score, detail=detail)


@pytest.mark.parametrize("visible", [Visibility.HIDDEN, Visibility.OBSERVE])
@pytest.mark.parametrize("selected", [False, True])
def test_visibility_and_selection_are_independent(tmp_path, visible, selected):
    primary = EvaluatorSpec("primary", lambda trace, ctx: EvalResult("primary", 2 - ctx.episode))
    audit = EvaluatorSpec("audit", lambda trace, ctx: EvalResult("audit", 100 * ctx.episode),
                         visibility=visible, selection_eligible=selected)
    goal = GoalConfig.of(evaluators=(primary, audit), selection="accept_reject")
    adapter = RecordingHarness()
    result = run(_cfg(tmp_path, adapter, goal, episodes=2))
    assert result.eval_history[1]["accepted"] is selected
    for prompt in adapter.prompts[2].values():
        assert ("audit:" in prompt) is (visible == Visibility.OBSERVE)
    assert len(result.eval_history[1]["results"]) == 2


def test_excluded_scores_do_not_change_resumed_baseline(tmp_path):
    goal = GoalConfig.of(evaluators=(
        EvaluatorSpec("score", lambda t, c: EvalResult("score", {1: 1., 2: .5, 3: .8}[c.episode])),
        EvaluatorSpec("audit", fixed(100), selection_eligible=False),
    ), selection="accept_reject")
    cfg = _cfg(tmp_path, RecordingHarness(), goal, episodes=2)
    assert [r["accepted"] for r in run(cfg).eval_history] == [True, False]
    resumed = run(replace(cfg, episodes=3), start=2)
    assert [r["accepted"] for r in resumed.eval_history] == [True, False, False]


@pytest.mark.parametrize("policy,expected", [("zero", 0.), ("missing", None)])
def test_evaluator_failure_is_not_a_selection_score(tmp_path, policy, expected):
    def unavailable(t, c):
        if c.episode == 2:
            raise RuntimeError("service unavailable api_key=synthetic_secret_value")
        return EvalResult("score", 0.2)
    goal = GoalConfig.of(evaluators=(EvaluatorSpec("score", unavailable,
        visibility=Visibility.OBSERVE, error_policy=policy),), selection="accept_reject")
    adapter = RecordingHarness()
    result = run(_cfg(tmp_path, adapter, goal))
    row = result.eval_history[1]
    assert not row["accepted"] and row["selection_unavailable"]
    assert row["results"][0]["score"] == expected
    assert row["results"][0]["status"] == "error"
    assert "synthetic_secret_value" not in json.dumps(result.eval_history)
    assert result.eval_history[2]["accepted"]
    for prompt in adapter.prompts[3].values():
        assert "evaluation unavailable" in prompt and "RuntimeError" in prompt
        assert "synthetic_secret_value" not in prompt


def test_schedule_initial_sparse_feedback_and_resume(tmp_path):
    calls = []
    def measure(t, c):
        calls.append(c.episode)
        return EvalResult("score", .8 - .1 * c.episode, detail="measured artifact")
    goal = GoalConfig.of(evaluators=(EvaluatorSpec("score", measure,
        visibility=Visibility.OBSERVE, every_n_episodes=3, include_initial=True),),
        selection="accept_reject")
    adapter = RecordingHarness()
    cfg = _cfg(tmp_path, adapter, goal, episodes=2)
    run(cfg)
    assert calls == [0]
    assert "initial H0" in adapter.prompts[1]["observe"]
    assert "older result" in adapter.prompts[2]["act"]
    run(replace(cfg, episodes=4), start=2)
    assert calls == [0, 3]
    history = json.loads(eval_history_path(cfg.root).read_text())
    assert [r["episode"] for r in history] == [1, 2, 3, 4]
    assert not history[2]["accepted"]  # compared against measured H0, not an empty baseline
    assert "episode 3" in adapter.prompts[4]["reflect"]
    assert "not kept" in adapter.prompts[4]["reflect"]
    assert (private_record_dir(cfg.root) / "initial_evaluation.json").exists()


def test_explicit_schedule_never_runs_unlisted_episodes():
    calls = []
    def ev(t, c):
        calls.append(c.episode)
        return EvalResult("v", 1)
    goal = GoalConfig.of(evaluators=(EvaluatorSpec("v", ev, episodes=(0, 2, 5)),))
    for ep in range(7):
        goal.evaluate([], GoalContext("unused", ep))
    assert calls == [0, 2, 5]


def test_skipped_evaluation_retains_prior_visible_error_only(tmp_path):
    def broken(t, c):
        raise RuntimeError("endpoint unavailable")
    goal = GoalConfig.of(evaluators=(
        EvaluatorSpec("visible", broken, visibility=Visibility.OBSERVE, episodes=(1,)),
        EvaluatorSpec("sealed", fixed(99, "PRIVATE_SENTINEL")),
    ))
    adapter = RecordingHarness()
    cfg = _cfg(tmp_path, adapter, goal, episodes=2)
    run(cfg)
    run(replace(cfg, episodes=3), start=2)
    for prompt in adapter.prompts[3].values():
        assert "endpoint unavailable" in prompt and "episode 1" in prompt
        assert "older result" in prompt
        assert "sealed" not in prompt and "PRIVATE_SENTINEL" not in prompt


@pytest.mark.parametrize("change", ["role", "visibility", "schedule"])
def test_changed_evaluator_contract_rejected_before_restore(tmp_path, change):
    spec = EvaluatorSpec("score", fixed())
    cfg = _cfg(tmp_path, RecordingHarness(), GoalConfig.of(evaluators=(spec,)), episodes=1)
    run(cfg)
    sentinel = cfg.root / "harness" / "uncommitted.txt"
    sentinel.write_text("do not touch")
    before = eval_history_path(cfg.root).read_bytes()
    opts = {"role": {"selection_eligible": False},
            "visibility": {"visibility": Visibility.OBSERVE},
            "schedule": {"every_n_episodes": 2}}[change]
    changed = replace(cfg, goal=GoalConfig.of(evaluators=(replace(spec, **opts),)), episodes=2)
    with pytest.raises(ValueError, match="contract differs"):
        run(changed, start=1)
    assert sentinel.read_text() == "do not touch"
    assert eval_history_path(cfg.root).read_bytes() == before


@pytest.mark.parametrize("opts", [
    {"selection_eligible": "false"}, {"every_n_episodes": 0}, {"every_n_episodes": True},
    {"episodes": (1, 1)}, {"episodes": (-1,)}, {"episodes": (True,)},
    {"episodes": (0,), "include_initial": True}, {"error_policy": "ignore"},
])
def test_policy_validation(opts):
    with pytest.raises(ValueError):
        EvaluatorSpec("x", fixed(), **opts)


def test_selection_requires_comparable_schedules():
    with pytest.raises(ValueError, match="share one schedule"):
        GoalConfig.of(evaluators=(EvaluatorSpec("a", fixed()),
            EvaluatorSpec("b", fixed(), every_n_episodes=3)), selection="accept_reject")
    with pytest.raises(ValueError, match="unique"):
        GoalConfig.of(evaluators=(EvaluatorSpec("a", fixed()), EvaluatorSpec("a", fixed())))


def test_common_feedback_channel_accepts_any_registered_producer():
    goal = GoalConfig.of(evaluators=(
        EvaluatorSpec("user-check", fixed(), visibility=Visibility.OBSERVE),
        EvaluatorSpec("agent-authored-check", fixed(), visibility=Visibility.OBSERVE),
        EvaluatorSpec("sealed", fixed()),
    ))
    channel = EvaluatorFeedback(goal)
    channel.record([EvalResult("user-check", 1), EvalResult("agent-authored-check", None,
                   status="error", detail="could not execute"), EvalResult("sealed", 100)],
                   episode=7, accepted=False, candidate_commit="a" * 40)
    text = channel.render(next_episode=9)
    assert "user-check" in text and "agent-authored-check" in text
    assert "could not execute" in text and "sealed" not in text
    assert "episode 7" in text and "not kept" in text


def test_cli_controls_and_manifest(tmp_path):
    from proteus.cli import main
    out = tmp_path / "sweep"
    assert main(["run", "--harness", "minimal", "--seeds", "1", "--episodes", "2",
        "--out", str(out), "--evaluator", "tool-calls@observe", "--eval-at", "tool-calls=0,2",
        "--selection-exclude", "tool-calls", "--selection", "accept_reject"]) == 0
    row = json.loads((out / "manifest.json").read_text())["evaluators"][0]
    assert row["selection_eligible"] is False and row["episodes"] == [0, 2]
    with pytest.raises(SystemExit, match="unknown evaluator"):
        main(["run", "--out", str(tmp_path / "invalid"), "--selection-exclude", "typo"])
    assert not (tmp_path / "invalid").exists()
