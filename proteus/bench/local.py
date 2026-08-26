"""A local, offline task pack — the cheap evaluator that proves the goal plumbing.

Docker-graded suites (see `swe.py`) are the high-fidelity option and cost tens of GB and a
container per instance. Most iteration does not need that: to ask whether a *goal* changes
what a harness evolves into, what matters is a stable scalar reward, not a leaderboard
number. This pack gives one — seeded bugs in a tiny repo, graded by running its tests, with
no network, no images, and no dataset download.

**Containment.** Grading runs code the agent wrote. That is exactly the capability that
escaped an application-level sandbox in our own runs, so the grader must run under the same
isolation as the episode: pass a `Sandbox`, or accept that `LocalSandbox` is no isolation
at all and only use it for harnesses you trust.
"""

from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path

from proteus.bench.task import BenchTask
from proteus.core.goal import EvalResult

GRADE_TIMEOUT_S = 60

#: The driver that runs a task's tests and reports a machine-readable tally. Kept as a
#: string so it is written into the task workspace at grade time, never imported here.
_DRIVER = '''\
import json, sys, traceback, importlib.util
spec = importlib.util.spec_from_file_location("t", "tests.py")
mod = importlib.util.module_from_spec(spec)
passed, failed = [], []
try:
    spec.loader.exec_module(mod)
except Exception:
    print(json.dumps({"passed": [], "failed": ["<import>"],
                      "error": traceback.format_exc()[-400:]}))
    sys.exit(0)
for name in sorted(n for n in dir(mod) if n.startswith("test_")):
    try:
        getattr(mod, name)()
        passed.append(name)
    except Exception:
        failed.append(name)
print(json.dumps({"passed": passed, "failed": failed}))
'''

# --------------------------------------------------------------------------- task pack

_TASKS = {
    "local:interval-merge": {
        "goal": ("The module `solution.py` in your `task/` directory merges overlapping "
                 "intervals, and its tests in `tests.py` do not all pass. Read the tests, "
                 "find the defect, and fix `solution.py` so every test passes. Do not edit "
                 "`tests.py`."),
        "solution": '''\
            def merge(intervals):
                """Merge overlapping intervals. [(1,3),(2,6)] -> [(1,6)]"""
                if not intervals:
                    return []
                out = [list(sorted(intervals)[0])]
                for start, end in sorted(intervals)[1:]:
                    # BUG: >= treats touching intervals as disjoint, e.g. (1,2),(2,3)
                    if start >= out[-1][1]:
                        out.append([start, end])
                    else:
                        out[-1][1] = max(out[-1][1], end)
                return [tuple(x) for x in out]
        ''',
        "tests": '''\
            from solution import merge

            def test_empty():
                assert merge([]) == []

            def test_disjoint():
                assert merge([(1, 2), (5, 6)]) == [(1, 2), (5, 6)]

            def test_overlapping():
                assert merge([(1, 3), (2, 6)]) == [(1, 6)]

            def test_touching():
                assert merge([(1, 2), (2, 3)]) == [(1, 3)]

            def test_unsorted_nested():
                assert merge([(5, 8), (1, 9), (2, 3)]) == [(1, 9)]
        ''',
    },
    "local:token-budget": {
        "goal": ("The module `solution.py` in your `task/` directory trims a list of "
                 "messages to fit a token budget, and its tests in `tests.py` do not all "
                 "pass. Read the tests, find the defect, and fix `solution.py` so every "
                 "test passes. Do not edit `tests.py`."),
        "solution": '''\
            def trim(messages, budget):
                """Keep the most recent messages that fit the budget, oldest dropped first.

                messages: list of (text, cost); returns the kept sublist in original order.
                """
                kept, total = [], 0
                # BUG: walks oldest-first, so it fills up on stale messages and drops the
                # recent ones the caller actually needs.
                for m in messages:
                    if total + m[1] <= budget:
                        kept.append(m)
                        total += m[1]
                return kept
        ''',
        "tests": '''\
            from solution import trim

            def test_all_fit():
                ms = [("a", 1), ("b", 2)]
                assert trim(ms, 10) == ms

            def test_keeps_recent():
                ms = [("old", 8), ("new", 2)]
                assert trim(ms, 5) == [("new", 2)]

            def test_order_preserved():
                ms = [("a", 3), ("b", 3), ("c", 3)]
                assert trim(ms, 6) == [("b", 3), ("c", 3)]

            def test_zero_budget():
                assert trim([("a", 1)], 0) == []
        ''',
    },
}


def _write_pack(ws: Path, key: str) -> None:
    spec = _TASKS[key]
    (ws / "solution.py").write_text(textwrap.dedent(spec["solution"]), encoding="utf-8")
    (ws / "tests.py").write_text(textwrap.dedent(spec["tests"]), encoding="utf-8")
    (ws / "README.md").write_text(
        f"# task\n\n{spec['goal']}\n", encoding="utf-8")
    # The local grader diffs nothing — it runs tests — so it needs no task-local git
    # repository. `<run>/task/` is deliberately outside the harness snapshot; benchmark
    # work moves forward while accept/reject selection applies only to the harness.


def _grade(ws: Path, key: str, *, sandbox=None) -> EvalResult:
    # grade against the held-out tests: restore tests.py from the spec first, so an agent
    # that "passes" by editing the tests (the reward-hack this setup invites) gains nothing
    (ws / "tests.py").write_text(textwrap.dedent(_TASKS[key]["tests"]), encoding="utf-8")
    (ws / "_grade.py").write_text(_DRIVER, encoding="utf-8")
    try:
        from proteus.bench.sandbox import run_python
        proc = run_python(ws, "_grade.py", timeout_s=GRADE_TIMEOUT_S, sandbox=sandbox)
    except subprocess.TimeoutExpired:
        return EvalResult(name=key, score=0.0, passed=False,
                          detail=f"grading timed out after {GRADE_TIMEOUT_S}s")
    finally:
        (ws / "_grade.py").unlink(missing_ok=True)
    try:
        rep = json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return EvalResult(name=key, score=0.0, passed=False,
                          detail=f"grader produced no report: {proc.stderr[-200:]}")
    n_ok, n_bad = len(rep["passed"]), len(rep["failed"])
    total = n_ok + n_bad
    score = n_ok / total if total else 0.0
    return EvalResult(name=key, score=score, passed=(n_bad == 0 and n_ok > 0),
                      detail=f"{n_ok}/{total} tests pass"
                             + (f"; failing: {rep['failed'][:3]}" if n_bad else ""))


def local_task(key: str) -> BenchTask:
    """One task from the built-in pack. `proteus.bench.local.TASKS` lists the keys."""
    if key not in _TASKS:
        raise KeyError(f"unknown local task {key!r}; have {sorted(_TASKS)}")
    return BenchTask(
        id=key,
        goal_text=_TASKS[key]["goal"],
        setup=lambda ws, k=key: _write_pack(ws, k),
        grade=lambda ws, sandbox=None, k=key: _grade(ws, k, sandbox=sandbox),
    )


TASKS = tuple(_TASKS)
