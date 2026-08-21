# Recipes

Copy-paste paths from a checkout of Proteus to measured self-evolution. The pinned Pi and
DeepSeek Harness paths below are the same paths exercised by `release-smoke.yml`.

## Install Proteus

Install the release from PyPI:

```bash
python3 -m pip install proteus-evolve
```

Clone the repository as well when a recipe uses the checked-in Dockerfiles or boot
wrappers under `environments/`:

```bash
git clone https://github.com/proteus-evolve/Proteus.git
cd Proteus
```

The offline reference harness needs no API key or Docker:

```bash
proteus run --harness minimal \
    --arm neutral --arm review:notes --arm review:tools \
    --seeds 4 --episodes 8 --out runs/demo
proteus reliability --harness minimal --out runs/demo
proteus measure --harness minimal --out runs/demo --travel
```

## Pi — evolve the real TypeScript source

[Pi](https://github.com/badlogic/pi-mono) is pinned at `v0.84.2`. Its source-mode image
bakes the checkout, dependencies, hydrated model data, and rebuild wrapper. The adapter
extracts that exact source into each run's `harness/src/`; changed source is rebuilt and
boot-checked before the next episode.

```bash
PI_BUILD_ROOT="$(mktemp -d)"
PI_CONTEXT="$PI_BUILD_ROOT/pi-mono"
git clone --depth 1 --branch v0.84.2 \
    https://github.com/badlogic/pi-mono "$PI_CONTEXT"
docker run --rm --network host -v "$PI_CONTEXT:/opt/src" -w /opt/src node:24-slim \
    sh -c 'npm ci --no-audit --no-fund && npm run hydrate:model-data'
docker run --rm -v "$PI_CONTEXT:/opt/src" --entrypoint sh node:24-slim \
    -c 'rm -rf /opt/src/node_modules /opt/src/packages/*/dist /opt/src/packages/*/*/dist'
cp environments/pi-src/boot.sh "$PI_CONTEXT/.proteus-boot.sh"
docker build -f environments/pi-src/Dockerfile \
    -t proteus-env-pi-src:0.84.2 "$PI_CONTEXT"

proteus check --harness pi
export DEEPSEEK_API_KEY=...
proteus check --harness pi --episode
proteus run --harness pi \
    --goal "Get better at your work, however you judge it." \
    --evaluator units:notes --evaluator step@observe --evaluator tool-calls \
    --arm neutral --arm review:notes \
    --seeds 2 --episodes 3 \
    --max-turns 32 --min-turns-per-phase 8 --announce-budget \
    --out runs/pi-demo
```

## DeepSeek Harness — evolve the real TypeScript source

[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) is pinned at
`dsh-v0.1.0-rc.8` (commit `141eb6fef83422698aef7a981029e843e8161534`).
The image and adapter use the same exact-tree source/rebuild contract as Pi. Python below
3.14 additionally needs `zstandard` to read DSH's multi-frame session logs.

```bash
python3 -m pip install 'zstandard>=0.21'
DSH_BUILD_ROOT="$(mktemp -d)"
DSH_CONTEXT="$DSH_BUILD_ROOT/deepseek-harness"
git clone --depth 1 https://github.com/deepseek-ai/deepseek-harness "$DSH_CONTEXT"
git -C "$DSH_CONTEXT" fetch --depth 1 origin tag dsh-v0.1.0-rc.8
git -C "$DSH_CONTEXT" checkout dsh-v0.1.0-rc.8
cp environments/dsh-src/boot.sh "$DSH_CONTEXT/.proteus-boot.sh"
docker build --network host -f environments/dsh-src/Dockerfile \
    -t proteus-env-dsh-src:0.1.0-rc.8 "$DSH_CONTEXT"

proteus check --harness dsh
export DEEPSEEK_API_KEY=...
proteus check --harness dsh --episode
proteus run --harness dsh \
    --goal "Get better at your work, however you judge it." \
    --evaluator units:notes --evaluator step@observe --evaluator tool-calls \
    --arm neutral --arm review:notes \
    --seeds 2 --episodes 3 \
    --max-turns 32 --min-turns-per-phase 8 --announce-budget \
    --out runs/dsh-demo
```

The versioned setup for evolving audio input from this exact baseline, including its
capability benchmark and public episode feed, is in
[`DSH_AUDIO_EVOLUTION.md`](DSH_AUDIO_EVOLUTION.md).

For both source adapters, an unchanged source takes a pristine fast path. A changed source
is exact-synced, rebuilt once per source hash, cached under `/state`, and rejected by the
post-reflect viability gate if it cannot boot. Every phase within an episode runs the same
frozen snapshot; a valid candidate activates only in the next episode, while an invalid one
is preserved and automatically rolled back. Containers run as the host uid/gid so their
bind-mounted files remain editable on Linux.

## Watch, measure, audit, resume, and export

These commands are harness-independent:

```bash
proteus watch --out runs/dsh-demo                 # http://localhost:8300/report.html
proteus reliability --harness dsh --out runs/dsh-demo
proteus audit --harness dsh --out runs/dsh-demo
proteus measure --harness dsh --out runs/dsh-demo --travel

# Re-running the same configuration after interruption skips finished seeds and resumes
# partial seeds after their last contiguous snapshot commit.
proteus run --harness dsh \
    --goal "Get better at your work, however you judge it." \
    --evaluator units:notes --evaluator step@observe --evaluator tool-calls \
    --arm neutral --arm review:notes \
    --seeds 2 --episodes 3 \
    --max-turns 32 --min-turns-per-phase 8 --announce-budget \
    --on-existing resume --out runs/dsh-demo

proteus repo export runs/dsh-demo/runs/run-<id> dsh-evolution
git -C dsh-evolution log --oneline
```

The resume invocation must use the same experimental configuration as the original run.
Resume restores the evolved files, selection baseline, visible feedback, and cumulative
counters; it does not reseed the harness.

## A benchmark as the goal

A benchmark run has two sibling workspaces:

- `<run>/harness/` is the evolving, measured subject and is snapshotted every episode.
- `<run>/task/` is the exercise. It lives outside the harness snapshot and moves forward;
  selection never rolls it back. DSH and Pi mount it at `/workspace/task`.

The CLI can seed one built-in local task or one Aider Polyglot exercise directly. A run may
attach one benchmark evaluator plus any number of measurement/custom evaluators:

```bash
proteus run --harness pi \
    --goal "Fix the interval merge implementation and make every test pass." \
    --evaluator local:interval-merge@observe \
    --evaluator step --evaluator tool-calls \
    --arm neutral --seeds 2 --episodes 10 --out runs/intervals

# First use shallow-clones and caches Aider-AI/polyglot-benchmark. Set
# PROTEUS_POLYGLOT_DIR to an existing checkout to avoid the clone.
proteus run --harness dsh \
    --goal "Implement the Bowling exercise and make every test pass." \
    --evaluator polyglot:bowling@observe \
    --evaluator step --arm neutral --seeds 2 --episodes 10 --out runs/bowling
```

The equivalent Python API, also used for third-party benchmarks, is:

```python
from pathlib import Path

from proteus.adapters.pi import PiHarness
from proteus.bench import as_goal
from proteus.bench.local import local_task
from proteus.core import NEUTRAL, RunConfig, Visibility, run

task = local_task("local:interval-merge")
cfg = RunConfig(
    name="intervals",
    adapter=PiHarness(),
    disposition=NEUTRAL,
    goal=as_goal(task, visibility=Visibility.OBSERVE),
    task=task,
    root=Path("runs/intervals-python"),
    model="",
    episodes=10,
)
run(cfg)
```

A custom adapter used with benchmarks must expose `<run>/task/` to its agent while keeping
it outside `harness/`; DSH and Pi demonstrate the container mount. The bundled `minimal`
and `llm` harnesses prove evaluator plumbing but do not offer general file tools for solving
arbitrary task workspaces.

### SWE-bench

`proteus.bench.swe.swe_task("django__django-11133")` grades through the official harness
(`make_test_spec` + `run_instance`). It reports the official binary `resolved` result as
`passed` and the fail-to-pass fraction as a dense `score`.

This integration is written against the verified upstream API but is not part of the live
release gate: it needs an x86_64 Linux host, roughly 120 GB free disk for per-instance
images, `pip install swebench datasets docker`, and network for the first pull. Its task
workspace is the instance repository at `base_commit`; the only bridge to the grader is
`git diff base_commit`, and the grader run id includes the episode because upstream caches
on `(run_id, instance_id)` rather than patch content.

## Goals and evaluators are independent

`--goal` is arbitrary natural-language objective text. Each repeatable `--evaluator` is a
separate measurement decision and is hidden by default; append `@observe` to show its result
to the agent in the next episode's observe phase.

Supported CLI evaluator forms:

- measurement: `units:<surface-name>`, `tool-calls`, `step`
- benchmark: `local:<task>`, `polyglot:<exercise>` (one benchmark per run)
- custom: `contains:<relpath>:<needle>`

A general goal such as “become more robust” needs no benchmark. A specific optimization
claim does: if the goal says “optimize benchmark X”, attach X and make it `@observe`, or the
agent has no measured feedback for the stated objective.

## Your harness

```bash
proteus env scaffold --from <git-url-or-local-path> --name yours --ref <tag-or-sha>
proteus env build yours
# Implement the adapter contract in docs/ADAPTERS.md, then:
proteus check --harness mypkg.yours_adapter:YoursHarness
proteus check --harness mypkg.yours_adapter:YoursHarness --episode
```
