"""Benchmark tasks as goals: the agent solves a task, the evaluator scores what it wrote.

A goal-conditioned run needs two workspaces that must not be confused:

* the **harness** (`<run>/harness/`) — what evolves and what we measure; it persists
  across episodes, is snapshotted per episode, and is the dependent variable;
* the **task workspace** (`<run>/task/`) — the thing the agent is asked to work on.

The task lives OUTSIDE the harness, beside it in the run root. It used to live inside,
and that was wrong twice over: a benchmark that clones a git repository (SWE-bench) put
a nested `.git` inside the snapshot, which git records as a gitlink — the task's working
files were invisible to snapshots and untouched by rejection rollbacks, so a "restored"
harness silently kept its evolved task state. Outside the snapshot, the contract is
explicit instead of accidental: **the task workspace is the exercise, not the subject —
it moves only forward, and selection rolls back the harness, never the task.**
Containerized adapters bind it into the agent's view at `/workspace/task`; the
measurement layer never reads it.

A `BenchTask` is three things: text the agent is given as its goal, a setup that seeds the
task workspace, and a grader that reads the workspace afterwards. Everything else — when
the goal text is shown, whether the score is visible to the agent, whether a regression is
rejected — is already the framework's job (`GoalConfig`).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from proteus.core.adapter import ActionEvent
from proteus.core.goal import EvalResult, Goal, GoalConfig, GoalContext, Visibility

#: Where a task is seeded, relative to the RUN root (the harness's parent).
TASK_SUBDIR = "task"


def task_root(harness_root: str | Path) -> Path:
    """The task workspace for a run, derived from the harness root every evaluator gets.

    `<run>/task/`, a sibling of `<run>/harness/` — outside the snapshot on purpose (see
    the module docstring)."""
    return Path(harness_root).parent / TASK_SUBDIR


@dataclass(frozen=True)
class BenchTask:
    """One benchmark instance, expressed in the three things a goal run needs."""

    id: str
    goal_text: str
    setup: Callable[[Path], None]
    grade: Callable[..., EvalResult]
    #: git ref the task workspace starts from, when the grader needs a diff
    base_commit: str = ""


def workspace_diff(ws: Path, base: str = "") -> str:
    """The agent's work as a git diff — the only thing most graders can consume.

    Falls back to a diff against the seeded state when no base commit is given (the local
    task pack commits its seed, so `HEAD` is that state).
    """
    ws = Path(ws)
    if not (ws / ".git").exists():
        return ""
    subprocess.run(["git", "-C", str(ws), "add", "-A"], capture_output=True, check=False)
    ref = base or "HEAD"
    out = subprocess.run(["git", "-C", str(ws), "diff", "--cached", ref],
                         capture_output=True, text=True, check=False)
    return out.stdout


def as_evaluator(task: BenchTask):
    """Wrap a task's grader in the framework's evaluator signature.

    Graders that accept an `episode` keyword get the current episode number — a grader
    with per-run caching (SWE-bench's official harness keys on run_id) must fold the
    episode into its cache identity or every episode returns episode 1's verdict."""
    import inspect
    takes_episode = "episode" in inspect.signature(task.grade).parameters
    takes_sandbox = "sandbox" in inspect.signature(task.grade).parameters

    def evaluate(trace: Sequence[ActionEvent], ctx: GoalContext) -> EvalResult:
        ws = task_root(ctx.harness_root)
        if not ws.exists():
            return EvalResult(name=task.id, score=0.0, passed=False,
                              detail="task workspace missing (was setup run?)")
        kwargs = {}
        if takes_episode:
            kwargs["episode"] = ctx.episode
        if takes_sandbox:
            kwargs["sandbox"] = ctx.grader_sandbox
        return task.grade(ws, **kwargs)

    return evaluate


def as_goal(task: BenchTask, *, visibility: Visibility = Visibility.HIDDEN,
            selection: str = "none") -> GoalConfig:
    """A single-goal config for this task, ready to hand to `RunConfig`."""
    return GoalConfig.single(
        Goal(name=task.id, text=task.goal_text, evaluator=as_evaluator(task),
             visibility=visibility),
        selection=selection,
    )


def seed_task(harness_root: str | Path, task: BenchTask) -> Path:
    """Materialise the task workspace. Call once, before the first episode."""
    ws = task_root(harness_root)
    ws.mkdir(parents=True, exist_ok=True)
    task.setup(ws)
    return ws
