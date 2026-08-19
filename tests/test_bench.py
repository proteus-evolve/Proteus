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
        task = local_task(key, trusted_local=True)
        ws = tmp_path / key.replace(":", "_")
        ws.mkdir()
        task.setup(ws)
        r = task.grade(ws)
        assert 0.0 < r.score < 1.0, f"{key} seeded at {r.score}"
        assert not r.passed


def test_grader_rewards_a_fix(tmp_path):
    task = local_task("local:interval-merge", trusted_local=True)
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
    # workspace_diff is the SWE-path bridge (a real git checkout); the local pack has no
    # git of its own (a nested .git would gitlink into the snapshot repo), so exercise the
    # diff against a plain committed repo
    import subprocess
    ws = tmp_path / "repo"
    ws.mkdir()
    (ws / "solution.py").write_text("def f():\n    return 0\n")
    (ws / "tests.py").write_text("from solution import f\n\ndef test_f():\n    assert f() == 1\n")
    for c in (["init", "-q"], ["add", "-A"],
              ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "seed"]):
        subprocess.run(["git", "-C", str(ws), *c], check=True, capture_output=True)
    assert workspace_diff(ws).strip() == ""      # nothing changed yet
    (ws / "solution.py").write_text("def f():\n    return 1\n")
    diff = workspace_diff(ws)
    assert "solution.py" in diff and "return 1" in diff
    assert "tests.py" not in diff


def test_local_grader_ignores_edited_tests(tmp_path):
    # editing tests.py must not raise the score: the grader restores held-out tests
    task = local_task("local:interval-merge", trusted_local=True)
    ws = tmp_path / "task"
    ws.mkdir()
    task.setup(ws)
    (ws / "tests.py").write_text(
        "def test_trivial():\n    assert True\n")      # agent tries to game it
    r = task.grade(ws)
    assert not r.passed and r.score < 1.0              # real tests were restored


def test_goal_conditioned_run_seeds_and_scores(tmp_path):
    task = local_task("local:interval-merge", trusted_local=True)
    cfg = RunConfig(name="goal", adapter=MinimalHarness(), disposition=NEUTRAL,
                    goal=as_goal(task, visibility=Visibility.OBSERVE),
                    root=tmp_path / "run", model="mock", episodes=2, seed=0, task=task)
    res = run(cfg)
    assert res.episodes_complete == 2
    ws = task_root(Path(res.root) / "harness")
    assert (ws / "solution.py").is_file()        # seeded before episode 1
    assert all(h["results"] for h in res.eval_history)   # scored every episode
    assert res.eval_history[0]["results"][0]["name"] == task.id


def test_local_task_refuses_implicit_host_execution():
    try:
        local_task("local:interval-merge")
    except ValueError as exc:
        assert "isolated sandbox" in str(exc)
    else:
        raise AssertionError("local_task allowed agent-authored code to run on the host")


def test_local_task_routes_grading_through_the_sandbox(tmp_path):
    import subprocess

    seen = {}

    class RecordingSandbox:
        def run(self, run_root, command, env, timeout_s, mounts=()):
            seen.update(run_root=run_root, command=command, env=env,
                        timeout_s=timeout_s, mounts=mounts)
            return subprocess.CompletedProcess(
                command, 0, '{"passed":["test_ok"],"failed":[]}\n', "")

    task = local_task("local:interval-merge", sandbox=RecordingSandbox())
    ws = tmp_path / "task"
    ws.mkdir()
    task.setup(ws)
    result = task.grade(ws)

    assert result.passed
    assert seen["command"] == ["python3", "/workspace/_grade.py"]
    assert seen["mounts"] == ((str(ws.resolve()), "/workspace"),)
