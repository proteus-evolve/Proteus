<p align="center">
  <img src="docs/assets/proteus-logo.png" alt="Proteus logo" width="340">
</p>

<h3 align="center">Self-evolution for any agent harness.</h3>

<p align="center">
  <b>Plug in. Evolve. Measure.</b>
</p>

<p align="center">
  <a href="https://github.com/yichen14/Proteus/actions/workflows/ci.yml"><img src="https://github.com/yichen14/Proteus/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green.svg" alt="MIT License"></a>
  <img src="https://img.shields.io/badge/python-3.10%2B-blue.svg" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/version-0.1.0-informational.svg" alt="v0.1.0">
  <img src="https://img.shields.io/badge/status-research%20preview-orange.svg" alt="research preview">
</p>

<p align="center">
  <a href="#-60-second-demo-no-api-key-no-docker">Quick Start</a> •
  <a href="#-harnesses-in-the-box">Harnesses</a> •
  <a href="#%EF%B8%8F-how-it-works">How It Works</a> •
  <a href="docs/ADAPTERS.md">Onboard Your Harness</a> •
  <a href="docs/RECIPES.md">Recipes</a> •
  <a href="environments/README.md">Environments</a> •
  <a href="#-measurement">Measurement</a>
</p>

---

Plug in *any* agent harness × *any* model, let it rewrite its own harness over many
context-fresh episodes, and measure **how the harness changes** — under a goal, many goals,
or no goal at all.

> Named for the sea-god who changes shape at will: Proteus watches a harness reshape
> itself, and gives you the ruler to measure the change.

## 🔭 Why Proteus is different

Agent self-improvement is moving from the weights to the **harness** — the prompts, memory,
skills, tools, and control loop the model runs on. Recent systems evolve a harness to raise
a benchmark score. Proteus asks a different, complementary question: **what does a
self-evolving harness actually *do*, and does an initial condition leave a permanent mark?**

Three things set it apart from every existing harness-evolution system:

1. **Harness-agnostic.** Others evolve harnesses built from their *own* primitives. Proteus
   evolves *yours*: implement one small `HarnessAdapter` and your agent — Aki (default),
   DeepSeek Harness, a bare ReAct loop, or your own — plugs into the same framework,
   sandbox, and measurement.
2. **Goal *and* no-goal, with visible or hidden evaluators.** Others hard-code a single
   regime: one benchmark verifier, agent blind to the score, goal mandatory. Proteus spans
   the space — `no-goal | one goal | many goals`, and evaluators the agent either **sees**
   (in the observe phase) or **never sees**. No-goal, unpressured evolution is a
   first-class mode.
3. **A measurement instrument, not just a score.** Others report task pass-rates. Proteus
   ships the ruler for the harness itself: **structural distance** between harness states
   (per surface, path length), a **crystallization / swap** test (remove the disposition,
   read the harness back), and **behavioural distance** with a permutation test (the
   action-preference statistic). Every condition is read with the same ruler.

## 🚀 60-second demo (no API key, no Docker)

```bash
pip install -e .            # dependency-free: even the live-LLM harness runs on stdlib
```

The bundled `minimal` harness runs fully offline, so you can see the whole pipeline before
wiring up a real agent:

```bash
proteus run --harness minimal \
    --arm neutral --arm review:notes --arm review:tools \
    --seeds 4 --episodes 8 --out runs/demo
proteus measure --harness minimal --out runs/demo
```

```
arm              seeds       notes       tools   (mean units built)
neutral              4         3.5         4.0
review_notes         4        13.0         0.0
review_tools         4         3.8         8.0

behavioural R (between/within arms, last episode): 3.075  p=0.0150
```

An installed action preference measurably shifts what the harness grows — and the same
`measure` reads a no-goal run and a goal run identically.

## 🧩 Harnesses in the box

| adapter | what it is | needs |
|---|---|---|
| `minimal` | offline reference harness (mock policy) | nothing |
| `llm` | the same harness driven by a live model — any OpenAI-compatible endpoint, DeepSeek by default | an API key |
| `dsh` | [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness), stock headless profile, in a prepared container | Docker + an explicit `gpt-*` model and repository-root OpenAI credential |
| `pi` | [Pi](https://github.com/badlogic/pi-mono) — Mario Zechner's minimal coding harness (4 tools, native AGENTS.md + skills) | Docker + a DeepSeek key |
| `aki` | the Aki research harness (the paper's apparatus) | the research checkout |
| yours | `--harness <module>:<Class>` — no registration | your adapter |

`dsh` is the template for third-party integrations: no harness code is modified. The
adapter seeds a workspace, supplies an ephemeral patch for the exact provider/model, launches
the prepared container per phase, and validates a new session log through terminal
`turn/end`. The DSH process receives only dummy route authentication; a controller-owned
OpenAI-compatible bridge holds the real credential and model provenance. Its disposition
installs as a removable block in `AGENTS.md`, which DSH reads natively. Stock rc.7 also
discovers candidate-writable project skills from `.dsh/skills/` and `.agents/skills/`.

## 🏗️ How it works

<p align="center">
  <img src="docs/assets/proteus-architecture.png" alt="The Proteus evolve loop" width="900">
</p>

Every seed runs `N` context-fresh **episodes**; only files cross the episode boundary. One
episode is four phases:

```
observe  →  propose  →  act  →  reflect
```

- **observe** — take stock; if you configured a *visible* evaluator, its score on the last
  episode is shown here.
- **propose** — list ways to improve your own harness.
- **act** — carry one out by editing the harness (the goal, if any, is announced here).
- **reflect** — decide what to keep.

The **framework** owns everything that is not the harness (prompts, goal text, evaluator
routing, snapshotting, selection, measurement). The **adapter** owns everything that is
(how the four phases actually execute). That split is what makes Proteus harness-agnostic.

### The core objects

| Concept | What it is |
|---|---|
| `HarnessAdapter` | the contract a harness implements: its surfaces, how to run an episode, how to read the action trace, how to install/remove a disposition |
| `Surface` | one editable, persistent region (memory / skills / tools / code / …), declared as data so the measurement layer needs no hard-coded names |
| `Disposition` | the action-preference perturbation — a **single, removable** change at t=0 (prompt suffix, config value, or code patch) |
| `GoalConfig` | goal / no-goal / multi-goal, each evaluator `HIDDEN` or `OBSERVE`-visible, plus outer-loop selection (`accept_reject`) |
| `Sandbox` | where an episode runs; `LocalSandbox` (trusted) or `DockerSandbox` (OS-level isolation, tunable network) |

### Action preference

An action preference is installed as a `Disposition` and is guaranteed **removable**, so the
crystallization test can take it away and read what the harness built on its own:

```python
from proteus.core import review, record, NEUTRAL
review("memory")     # each phase: review your memory, act or not
record("tools")      # keep your tools current as you work
NEUTRAL              # the control, F0 — no perturbation
```

### Goals and evaluators

```python
from proteus.core import GoalConfig, Goal, Visibility

GoalConfig.no_goal()                                    # unpressured evolution
GoalConfig.single(Goal("solve", text=..., evaluator=my_eval,
                       visibility=Visibility.OBSERVE))  # agent sees its score
GoalConfig.multi([...])                                 # several objectives at once
GoalConfig.single(goal, selection="accept_reject")      # outer loop rejects regressions
```

An evaluator is any callable `(trace, ctx) -> EvalResult`; bring a benchmark verifier, an
LLM judge, or one of the built-ins (`proteus.core.evaluators`).

### Sandbox

```python
from proteus.sandbox import SandboxConfig, DockerSandbox
DockerSandbox(SandboxConfig(network="none"))    # no egress
DockerSandbox(SandboxConfig(network="host",     # needs an LLM endpoint
                            env_passthrough=("OPENAI_API_KEY",),
                            mem_limit="4g"))
```

A self-editing agent writes and runs its own code, so an application-level file sandbox
cannot contain it — Proteus runs real harnesses in a container whose filesystem holds the
harness and nothing else. Docker environment values travel in the Docker client process
environment; the host command line contains only `-e NAME`, never `-e NAME=value`.

## 🔌 Onboard your harness

The input is a **repository** — a git URL or local path:

```bash
proteus env scaffold --from https://github.com/org/their-harness --name theirs --ref v1.2.0
proteus env build theirs             # pinned image, resolved sha recorded in the manifest
# write the adapter (7 methods), then:
proteus check --harness mypkg.theirs_adapter:TheirsHarness --episode --model <model>
proteus run   --harness mypkg.theirs_adapter:TheirsHarness --arm neutral ...
```

`proteus check` machine-verifies the contract (removable disposition via fingerprint
round-trip, snapshot-ability, trace shape). The full guide: [docs/ADAPTERS.md](docs/ADAPTERS.md).

## 📦 Prepared environments

`environments/` ships one pinned Docker environment per supported harness — a `Dockerfile`
plus an `environment.toml` manifest (`SandboxConfig.from_manifest` loads it). The evolving
workspace is always a mount, never baked into the image, so one image serves every
condition and seed. Conventions: [environments/README.md](environments/README.md); design
survey behind them (what we borrowed from Harbor): [docs/ENVIRONMENTS.md](docs/ENVIRONMENTS.md).

## 📏 Measurement

```python
from proteus.measure import distance, stream, crystallize
```

- `distance` — structural distance per surface (added / dropped / revised), path length
  (`proteus measure --travel`).
- `stream` — behavioural distance (frequency / order / procedure) and the between/within
  permutation test `R`.
- `crystallize` — mount an evolved state under a neutral disposition and test whether it
  reads back as its own endpoint (two-stage fidelity + arm-shift).

## 📤 Outputs

Every run's primary artifact is its **evolution history as a git repository** — one commit
per episode. Keep it local, browse it, or push it wherever you like (never automatic):

```bash
proteus repo export runs/demo/runs/run-<id> my-evolution   # normal repo, `git log` = the trajectory
proteus repo push   runs/demo/runs/run-<id> git@github.com:you/my-evolution.git
```

Every sweep also ships a **live tracking page** — per-run progress, per-surface growth
curves, evaluator scores — updating while the sweep runs:

```bash
proteus watch --out runs/demo          # http://localhost:8300/report.html
```

Tracking data (condition labels, hidden scores) lives at the sweep level, outside run
roots, so the evolving agent can never read its own condition.

## 🛡️ Candidate safety and integrity auditing

Proteus keeps task selection, online candidate safety, and post-run instrument auditing
separate. Task evaluators may affect selection or provide configured feedback. Safety
indicators can reject a frozen candidate, but their values, names, evidence, blockers, and
warnings never enter prompts, reward, critique, memory, or later episodes.

Enable the Phase 1 activation gate explicitly:

```bash
uv run proteus run \
  --harness aki \
  --arm neutral \
  --goal none \
  --seeds 1 \
  --episodes 3 \
  --model gpt-5.6-luna \
  --safety-suite proteus.safety.phase1:SUITE \
  --out runs/evolution-safety-phase1
```

The ownership chain is deliberately narrow: `HarnessAdapter` runs ordinary evolution;
the optional `CandidateSafetyAdapter` exposes a safety profile and native executor;
`CandidateSafetyExecutor` administers adapter-native probes; and `GateRunner` owns the
shared matched-cell schedule, validation, indicator derivation, policy, and publication.
A configured safety suite always runs in full before activation; it cannot select a family
subset. Preflight validates the suite, adapter bindings, live-model budget, and
repository-root `.env` credential before creating the sweep directory. There is no
best-effort, policy-selection, scalar-threshold, or safety-feedback option.

Every evolved tree is frozen before task and safety evaluation. Proteus activates it only
when both decisions allow; otherwise it restores the previous active tree while preserving
the rejected candidate and controller evidence. Terminal artifacts live under
`<sweep>/safety-gates/`, outside subject run roots. `proteus report --out <sweep>` renders
logical active/candidate identity, task selection, activation outcome, five independent
indicator profiles, blockers, warnings, and artifact links. It does not compute a combined
safety score or describe a rejected candidate as active.

The Aki and DSH adapters structurally implement `CandidateSafetyAdapter` and return their
native `CandidateSafetyExecutor`. Aki executes only each materialized endpoint's native
`loop.py::run_episode(ctx)` inside a keyless, network-denied worker. An explicit Aki ordinary-run
model that differs from its native binding fails before the supervisor runs. The trusted controller
owns model credentials and API calls. Candidate-authored events cannot establish permission
containment or recovery. Missing Aki or DSH native loader, permission, maintenance, lineage, or
recovery evidence remains `not_exposed` or `not_evaluated` and therefore blocks critical
activation; Proteus does not install a fallback.

The deterministic integrity audit remains a separate post-run command:

```bash
proteus audit --harness minimal --out runs/demo \
    --audit-id instrument-integrity-v1
proteus report --out runs/demo
```

Audit artifacts live under `<sweep>/audits/`. The default `instrument-integrity` suite
checks snapshot and trace observability; it does not establish harness safety. Audit
outcomes preserve `not_evaluated`, `invalid`, and `error`, and audit results never affect
selection or later episodes.

## 📊 Status

`v0.1` (research preview). Working today: the offline `minimal` harness with the full
measurement suite; the `llm` harness live against DeepSeek; the `dsh` adapter running
keyless, explicitly model-bound DeepSeek Harness headless episodes in its prepared container;
the `aki` adapter — measure
path reads existing research runs with no checkout, run path drives the containerized
research runner; repo-first environment builds; the adapter compliance checker; CI on
Python 3.10–3.14. As a cross-implementation check, Proteus's behavioural ruler applied to
the research runs independently reproduces their headline dynamics: arms separate at
episode 1 (R = 1.63) and converge by episode 30 (R = 0.93). Proteus is the open framework
behind our paper on action preference as an initial condition for self-improving agents.

## 📖 Citation

See [CITATION.cff](CITATION.cff). A paper reference will be added when the preprint is
public.

## License

MIT.
