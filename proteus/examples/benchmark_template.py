"""Template: a new benchmark, expressed as a `BenchTask`, ready to copy.

A benchmark in Proteus is three things (see `proteus/bench/task.py`): the **goal text**
the agent is given, a **setup** that seeds the task workspace, and a **grader** that reads
the workspace afterwards and returns an `EvalResult`. Wrap it with `as_goal(task)` to hand
to `RunConfig` (the agent works the task while its harness evolves), or `as_evaluator(task)`
to score without conditioning. `proteus.bench.local` / `polyglot` / `swe` are the shipped
instances; this is the smallest one that shows the whole shape.

    python -m proteus.scaffold benchmark <yourname>   # copy + rename this file
    python -m proteus.examples.benchmark_template             # run the demo below, offline

The task workspace (`<run>/task/`) lives OUTSIDE the harness snapshot on purpose: it is
the exercise, not the subject — it moves only forward, and selection rolls back the
harness, never the task. Do not write task files into the harness root.

SECURITY — graders execute agent-authored code. If `grade` declares a `sandbox`
parameter, Proteus injects the episode's grader sandbox (`ctx.grader_sandbox`) and you run
the agent's code under the same isolation as the episode via `proteus.bench.sandbox.run_python`
— which never falls back to host Python. That is the production path (`proteus/bench/local.py`
and `polyglot.py` both use it). The host-subprocess branch below runs only when no sandbox
is supplied, so the template stays demonstrable offline; it is NOT isolation and must not
be the path a real, self-editing harness is graded through.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

from proteus.bench.task import BenchTask
from proteus.core.goal import EvalResult

#: The checks the agent's solution must satisfy, as (call, expected) pairs. Keep them out
#: of the workspace so the agent cannot read the answer key.
_CHECKS = [("add(2, 3)", 5), ("add(-1, 1)", 0), ("add(0, 0)", 0)]

GOAL_TEXT = textwrap.dedent("""\
    In this workspace, edit `solution.py` so that it defines a function `add(a, b)`
    returning the sum of its two arguments. Do not change anything else.
""")


def _probe_src() -> str:
    """The verifier script: imports the agent's solution and asserts the hidden checks."""
    return "import solution\n" + "".join(
        f"assert solution.{call} == {expected}, {call!r}\n" for call, expected in _CHECKS)


def _setup(ws: Path) -> None:
    """Seed the task workspace: a spec, an empty stub, and a committed baseline for diffs.

    TODO: for a real benchmark, clone/copy the instance's files here (SWE-bench clones a
    repo at `base_commit`; the local pack copies a prompt + a held-out test it restores
    before grading). Commit the seed so `workspace_diff` can show the agent's work.
    """
    (ws / "README.md").write_text(GOAL_TEXT)
    (ws / "solution.py").write_text("# TODO: define add(a, b)\n")
    subprocess.run(["git", "-C", str(ws), "init", "-q"], check=False)
    subprocess.run(["git", "-C", str(ws), "add", "-A"], check=False, capture_output=True)
    subprocess.run(["git", "-C", str(ws), "commit", "-q", "-m", "seed"],
                   check=False, capture_output=True)


def _grade(ws: Path, *, sandbox=None) -> EvalResult:
    """Run the agent's solution against the hidden checks; score = fraction passed.

    Declaring the `sandbox` parameter is what opts this grader into isolated execution:
    `as_evaluator` sees it and passes `ctx.grader_sandbox`. Swap the checks below for your
    benchmark's real verifier. Always degrade a broken/absent solution to a legible 0
    rather than raising, so one bad episode is a recorded 0, not a crashed sweep.
    """
    if not (ws / "solution.py").exists():
        return EvalResult(name="template-add", score=0.0, passed=False,
                          detail="solution.py missing")
    if sandbox is not None:
        # Production path: execute the agent's code under the episode's isolation.
        from proteus.bench.sandbox import run_python
        (ws / "_probe.py").write_text(_probe_src())
        proc = run_python(ws, "_probe.py", timeout_s=30, sandbox=sandbox)
    else:
        # Offline/demo fallback ONLY — the host process, i.e. no isolation. See the
        # SECURITY note; a real harness is graded through the sandbox branch above.
        proc = subprocess.run([sys.executable, "-c", _probe_src()], cwd=str(ws),
                              capture_output=True, text=True, timeout=30)
    if proc.returncode == 0:
        return EvalResult(name="template-add", score=1.0, passed=True, detail="all checks pass")
    return EvalResult(name="template-add", score=0.0, passed=False,
                      detail=((proc.stderr or "").strip().splitlines() or ["failed"])[-1])


#: The task object. `as_goal(TASK)` / `as_evaluator(TASK)` plug it into the framework.
TASK = BenchTask(id="template-add", goal_text=GOAL_TEXT, setup=_setup, grade=_grade)


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        ws = Path(d)
        _setup(ws)
        print("empty stub :", _grade(ws))                       # -> score 0.0
        (ws / "solution.py").write_text("def add(a, b):\n    return a + b\n")
        print("solved     :", _grade(ws))                       # -> score 1.0
