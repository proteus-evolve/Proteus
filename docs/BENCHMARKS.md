# Bringing a benchmark

A benchmark in Proteus is a **goal with a grader**: the agent is told what to pursue, and
between episodes the grader scores what is actually in the workspace. The whole contract
is one `BenchTask` — three things:

If you only need to observe the harness or compare runs—without seeding an exercise—add a
[measurement](MEASUREMENTS.md) instead.

```python
BenchTask(
    id="yourbench:task-17",
    goal_text="What the agent is told to pursue.",
    setup=lambda ws: ...,   # write the task into the agent's task/ workspace, once
    grade=lambda ws: ...,   # -> EvalResult(score in [0,1], passed, detail), every episode
)
```

Everything else — seeding into the harness, per-evaluator visibility, selection, progress
records, the tracking page — is the framework's job and works the same for every
benchmark. Wire it into a run with:

```bash
# Built-in local task; benchmark evaluators seed <run>/task/ automatically.
proteus run --harness pi \
    --goal "Fix the interval merge implementation." \
    --evaluator local:interval-merge@observe \
    --arm neutral --seeds 2 --episodes 10 --out runs/intervals

# External Polyglot exercise; the dataset is shallow-cloned and cached on first use.
proteus run --harness pi \
    --goal "Implement the Bowling exercise and pass its tests." \
    --evaluator polyglot:bowling@observe \
    --arm neutral --seeds 2 --episodes 10 --out runs/bowling

# Sanitized MBPP task; one JSON file is downloaded and cached on first use.
proteus run --harness pi \
    --goal "Solve MBPP task 2 in task/solution.py." \
    --evaluator mbpp:2@observe \
    --arm neutral --seeds 2 --episodes 10 --out runs/mbpp-2

# OpenAI HumanEval task; the official gzipped JSONL is downloaded and cached.
proteus run --harness pi \
    --goal "Solve HumanEval/0 in task/solution.py." \
    --evaluator humaneval:HumanEval/0@observe \
    --arm neutral --seeds 2 --episodes 10 --out runs/humaneval-0
```

The CLI accepts at most one benchmark evaluator per run because a run has one task
workspace. Measurement and custom evaluators remain repeatable alongside it. The equivalent
Python API is:

```python
from proteus.bench import as_goal
task = ...
cfg = RunConfig(..., goal=as_goal(task, visibility=Visibility.OBSERVE), task=task)
```

The framework seeds `<run>/task/`, but the adapter owns agent I/O: an adapter that supports
benchmarks must expose that sibling workspace without moving it into `harness/`. The built-in
DSH and Pi adapters bind it at `/workspace/task`, and benchmark goal text directs the agent
to that `task/` directory.

## The three tiers

| tier | module | needs | use it for |
|---|---|---|---|
| built-in | `proteus.bench.local` | nothing | smoke tests, plumbing, CI |
| **lightweight external** | `proteus.bench.polyglot` | Python + one shallow clone | real ablations at negligible cost |
| **lightweight external** | `proteus.bench.mbpp` | Python + one cached JSON file | short, dense-scored synthesis tasks |
| **lightweight external** | `proteus.bench.humaneval` | Python + one cached JSONL | official binary synthesis checks |
| heavyweight official | `proteus.bench.swe` | Docker, ~120 GB, x86_64 | headline numbers on the real thing |

**Start from `polyglot.py`** — it is deliberately the worked example. ~180 lines, no
dependencies, and it demonstrates every property a contributed benchmark must have.
`tests/test_polyglot.py` fabricates a miniature dataset in the benchmark's exact layout,
so it also documents the shape your loader must read, and your tests never touch the
network.

MBPP uses Google Research's Apache-2.0 `sanitized-mbpp.json`. Set
`PROTEUS_MBPP_PATH=/path/to/sanitized-mbpp.json` to use an existing copy; otherwise the
file is cached under `~/.cache/proteus/mbpp/`. The prompt is seeded into the task, while
the reference implementation and assertions remain held out by the grader.

HumanEval uses OpenAI's MIT-licensed `HumanEval.jsonl.gz`. Set
`PROTEUS_HUMANEVAL_PATH=/path/to/HumanEval.jsonl.gz` to use an existing copy; otherwise
the file is cached under `~/.cache/proteus/humaneval/`. The public prompt is seeded into
`solution.py`; the canonical solution and official `check(candidate)` function stay in
the isolated grader. HumanEval's official per-sample verdict is binary, so its Proteus
score is `0.0` or `1.0` rather than MBPP's per-assert fraction.

## What a contributed benchmark must get right

Each of these exists because an agent found the exploit or a run hit the failure:

1. **The seeded task must start failing.** A task whose stub already passes is a dead
   reward signal (`test_lists_and_seeds_a_failing_exercise`).
2. **Tests are held out.** `grade` rewrites the test files from the dataset before running
   them. Agents edit tests when that raises the score — ours did
   (`test_editing_the_tests_gains_nothing`).
3. **Grading is cwd-independent.** Resolve every path from the mounted task workspace,
   never from the framework process's working directory. Local and Polyglot graders run
   in a networkless, read-only Docker sandbox with only `/task` mounted.
4. **Keep the task outside the measured snapshot.** `task_root(harness_root)` resolves to
   `<run>/task/`, a sibling of `<run>/harness/`. Containerized adapters mount it at
   `/workspace/task`. This lets SWE-bench keep its own `.git` repository without turning it
   into a gitlink in the harness snapshot; selection rolls back the harness, never the task.
5. **Grading runs agent-authored code — contain it.** Route execution through the grader
   sandbox (`GoalContext.grader_sandbox`); never invoke the host Python as a fallback.
   If a secure grader is unavailable, return a legible zero without executing the code.
6. **Heavy dependencies are imported lazily and fail legibly.** The framework must run
   without your benchmark's SDK installed; a missing dependency is a scored zero with an
   install hint, not an ImportError at import time (`swe.py` shows the pattern).
7. **Dense scores over binary ones where the benchmark allows.** An evolution study reads
   direction; `7/31 tests` says more than `failed`. Report the official binary verdict
   through `passed` and the dense fraction through `score`.
8. **Attribute the dataset.** Name the source and its license in the module docstring
   (polyglot: © Exercism, per the benchmark repo's README).

## Checklist for a PR

- [ ] `BenchTask` factory (`yourbench_task(id, ...)`) + `list_tasks()` for discovery
- [ ] offline tests against a fabricated miniature dataset in the real layout
- [ ] a real-dataset spot check documented in the PR description (stub fails, reference
      solution passes)
- [ ] the eight properties above, each visible in the code or the tests
- [ ] module docstring says which tier it is and what it costs to run
