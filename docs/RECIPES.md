# Recipes

Complete, copy-paste sequences from a stock harness to measured self-evolution. Execution
status is stated per recipe.

## Pi (pi-coding-agent) — minimal harness, full pipeline

[Pi](https://github.com/badlogic/pi-mono) is Mario Zechner's deliberately minimal coding
harness: four built-in tools, native `AGENTS.md`, native skills. Nothing in it knows about
Proteus — which is the point.

Status: steps 1–2 executed (image built; `proteus check --harness pi` passes 8/8 static +
provisioning; the container reaches the DeepSeek endpoint and writes session JSONL).
Steps 3–6 are wired but not yet executed live.

```bash
# 1. prepared environment (Node 24 + pi, pinned)
docker build -q -t proteus-env-pi:0.84.2 environments/pi/

# 2. contract check (free: static + provisioning; --episode adds one live episode)
proteus check --harness pi
proteus check --harness pi --episode          # needs DEEPSEEK_API_KEY

# 3. self-evolution: two arms, no goal
export DEEPSEEK_API_KEY=...
proteus run --harness pi \
    --arm neutral --arm review:notes \
    --seeds 2 --episodes 3 --out runs/pi-demo

# 4. watch it live (from a second terminal, any time after step 3 starts)
proteus watch --out runs/pi-demo              # http://localhost:8300/report.html

# 5. measure with the same ruler every harness gets
proteus measure --harness pi --out runs/pi-demo --travel

# 6. keep the evolution history as a git repo; push it if you want
proteus repo export runs/pi-demo/runs/run-<id> pi-evolution
git -C pi-evolution log --oneline             # one commit per episode
```

What the adapter does (~150 lines, `proteus/adapters/pi.py`): seeds `AGENTS.md` +
`notes/ tools/ skills/`, installs the disposition as a removable marked block in
`AGENTS.md` (pi loads it natively), runs one `pi -p` session per phase in the container
(`--session-dir` pointed at a mounted state dir, `--skill /workspace/skills`), and parses
pi's session JSONL (`message` events, `toolCall` blocks) into the normalized trace.

## DeepSeek Harness (dsh) — plugin-architecture harness

Same shape, different harness (see `proteus/adapters/dsh.py`; environment in
`environments/deepseek-harness/`). Executed 2026-08-17: 2 arms x 2 episodes, all complete;
both agents edited their own `AGENTS.md`; the installed disposition block survived on the
review arm and the fingerprint separated the arms.

```bash
docker build -q -t proteus-env-dsh:0.1.0-rc.7 environments/deepseek-harness/
proteus run --harness dsh --arm neutral --arm review:notes \
    --seeds 1 --episodes 2 --out runs/dsh-demo
proteus measure --harness dsh --out runs/dsh-demo
```

## Your harness

```bash
proteus env scaffold --from <git-url-or-local-path> --name yours
proteus env build yours
# write the adapter (docs/ADAPTERS.md), then:
proteus check --harness mypkg.yours_adapter:YoursHarness --episode
```

## With-goal ablation — benchmarks as goals

Proteus's goal axis becomes an experiment when the goal is a *task* and the evaluator is
its grader. `proteus.bench` supplies both from one object:

```python
from proteus.bench import as_goal
from proteus.bench.local import local_task
from proteus.core import Visibility
from proteus.core.episode import RunConfig, run
from proteus.sandbox import DockerSandbox, SandboxConfig

grader = DockerSandbox(SandboxConfig(image="python:3.12-slim", network="none"))
task = local_task("local:interval-merge", sandbox=grader)  # no dataset; agent code stays isolated
cfg = RunConfig(name="goal-observe", adapter=..., disposition=...,
                goal=as_goal(task, visibility=Visibility.OBSERVE),
                root=..., model=..., episodes=30, task=task)
run(cfg)
```

The task is seeded into `harness/task/` before episode 1 and graded after every episode, so
the ablation is a matter of swapping one field:

| condition | `goal=` |
|---|---|
| no goal (this paper's primary regime) | `GoalConfig.no_goal()` |
| goal, score hidden from the agent | `as_goal(task)` |
| goal, score shown in the next observe phase | `as_goal(task, visibility=Visibility.OBSERVE)` |
| goal + outer-loop rejection of regressions | `as_goal(task, selection="accept_reject")` |

**Two workspaces, do not confuse them.** `harness/` is what evolves and what the rulers
measure; `harness/task/` is what the agent was asked to work on. Task files land inside the
harness workspace on purpose — every adapter already gives the agent file access there, so
a benchmark needs no adapter change — but that means the measurement layer counts them as
structure unless you exclude `TASK_SUBDIR`.

### SWE-bench

`proteus.bench.swe.swe_task("django__django-11133")` grades through the official harness
(`make_test_spec` + `run_instance`), reporting the binary `resolved` as `passed` and the
fail-to-pass fraction as `score` (a sparse 0/1 reward says little about *direction*, which
is what an evolution study reads).

Written against the verified upstream API; **not yet executed** — it needs an x86_64 Linux
box with ~120 GB free disk (per-instance images), `pip install swebench datasets docker`,
and network for the first pull. Three constraints are load-bearing and documented in the
module: the only bridge to the grader is `git diff base_commit` (so the task workspace must
be that repo at that commit — `setup` clones it), the upstream cache keys on
`(run_id, instance_id)` and ignores the patch (so the run id embeds the episode), and every
distinct instance is another image (so pin a small fixed set).

## Goals and evaluators are separate decisions

What you *tell* the agent and what you *measure* are independent. The goal is freeform
text; evaluators attach on their own, each with its own visibility:

```bash
proteus run --harness llm \
    --goal "Make yourself more robust." \
    --evaluator units:notes@observe \
    --evaluator step \
    --arm neutral --seeds 2 --episodes 10 --out runs/robust
```

Every evaluator runs to completion between episodes — after episode N ends, before N+1
starts. `@observe` results are shown to the agent at the start of its next episode;
`@hidden` (the default) results go only to the run's records — progress lines,
`eval_history.json`, and the tracking page — so the user always sees everything, and the
agent sees exactly what the condition says it may.

Two families of evaluator, because they answer different questions:

- **measurement** — the study's own instruments: `units:<surface>` (what has been built),
  `step` (structural movement since the previous episode), `tool-calls`. Cheap, intrinsic,
  defined for every harness; a no-goal run is read entirely with these. Crystallization
  stays a sweep-level probe rather than a per-episode evaluator: one administration is a
  full LLM episode, so it is run at checkpoints, not between every pair of episodes.
- **benchmark** — external ground truth: the local task pack (`proteus.bench.local`),
  SWE-bench (`proteus.bench.swe`). Attach via the Python API (`as_goal(task, ...)`), which
  also seeds the task into the workspace.

A general goal ("more robust") needs no benchmark — the measurement evaluators will show
what the harness did with the words. **A specific goal must come with its measure**: if
the goal says "optimize SWE-bench", then SWE-bench must actually be attached as an
evaluator and set `@observe`. A specific goal with no visible measure gives the agent an
objective it can neither pursue nor verify, and what you get is drift toward whatever the
text connotes rather than optimization of anything.
