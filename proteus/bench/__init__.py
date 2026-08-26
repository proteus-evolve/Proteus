"""Benchmarks as goals: task packs whose graders are Proteus evaluators."""
from proteus.bench.task import (
    TASK_SUBDIR,
    BenchTask,
    as_evaluator,
    as_goal,
    seed_task,
    task_root,
    workspace_diff,
)

__all__ = [
    "TASK_SUBDIR",
    "BenchTask",
    "as_evaluator",
    "as_goal",
    "seed_task",
    "task_root",
    "workspace_diff",
]

from proteus.bench.polyglot import list_tasks, polyglot_task  # noqa: E402,F401
__all__ += ["list_tasks", "polyglot_task"]
