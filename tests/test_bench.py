"""Benchmark tasks as goals: seeding, grading, and the goal-conditioned run path."""

from pathlib import Path

from proteus.adapters.minimal import MinimalHarness
from proteus.bench import as_goal, task_root, workspace_diff
from proteus.bench.local import TASKS, local_task
from proteus.core import NEUTRAL, Visibility
from proteus.core.episode import RunConfig, run


def test_seeded_tasks_start_failing(tmp_path):
    # a task whose seed already passes is a dead reward signal
    for key in TASKS:
        task = local_task(key)
        ws = tmp_path / key.replace(":", "_")
        ws.mkdir()
        task.setup(ws)
        r = task.grade(ws)
        assert 0.0 < r.score < 1.0, f"{key} seeded at {r.score}"
        assert not r.passed


def test_grader_rewards_a_fix(tmp_path):
    task = local_task("local:interval-merge")
    ws = tmp_path / "task"
    ws.mkdir()
    task.setup(ws)
    before = task.grade(ws)
    # apply the fix an agent would have to find
    src = (ws / "solution.py").read_text()
    (ws / "solution.py").write_text(src.replace("start >= out[-1][1]",
                                                "start > out[-1][1]"))
    after = task.grade(ws)
    assert after.score > before.score
    assert after.passed and "5/5" in after.detail


def test_diff_shows_only_the_agents_edit(tmp_path):
    task = local_task("local:token-budget")
    ws = tmp_path / "task"
    ws.mkdir()
    task.setup(ws)
    assert workspace_diff(ws).strip() == ""      # nothing changed yet
    (ws / "solution.py").write_text("# rewritten\n")
    diff = workspace_diff(ws)
    assert "solution.py" in diff and "# rewritten" in diff
    assert "tests.py" not in diff


def test_goal_conditioned_run_seeds_and_scores(tmp_path):
    task = local_task("local:interval-merge")
    cfg = RunConfig(name="goal", run_id="run-bench", adapter=MinimalHarness(), disposition=NEUTRAL,
                    goal=as_goal(task, visibility=Visibility.OBSERVE),
                    root=tmp_path / "run", model="mock", episodes=2, seed=0, task=task)
    res = run(cfg)
    assert res.episodes_complete == 2
    ws = task_root(Path(res.root) / "harness")
    assert (ws / "solution.py").is_file()        # seeded before episode 1
    assert all(h["results"] for h in res.eval_history)   # scored every episode
    assert res.eval_history[0]["results"][0]["name"] == task.id
