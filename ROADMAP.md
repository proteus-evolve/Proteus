# Proteus Roadmap

Proteus provides infrastructure for an agent harness to evolve its own instructions,
memory, skills, tools, and source code, with a goal, multiple goals, or no predefined goal.
Its roadmap has **four parallel development directions**: runtime infrastructure,
developer experience, measurement and visualization, and ecosystem integrations.
All four can advance concurrently; their order here does not express priority.

Status as of **2026-10-01**:

- **Completed** — implemented on the public repository's `main` branch; availability in
  a published package follows the release notes.
- **In progress** — an active implementation is linked.
- **Planned** — an intended improvement or candidate integration, without a promised date.

## Runtime infrastructure

**Goal:** make long-running self-evolution reliable, recoverable, and reproducible.

| Status | Work |
|---|---|
| Completed | Staged activation for source-evolving adapters: each episode runs a frozen active snapshot, writes a separate candidate, and validates it before activation in the next episode. Failed candidates remain available for repair. |
| Completed | Framework handoff and persistent controller notices across fresh contexts; producer-neutral feedback for registered evaluators, filtered by visibility. |
| Completed | Independent evaluator visibility and selection eligibility; scheduled evaluation, optional initial H0 evaluation, and explicit error/unavailable result status. |
| Completed | Configurable episode phases and prompts, phase budgets, and opt-in DSH interrupted-phase recovery under the original budget and runtime contract. |
| In progress | Correct Codex native-call accounting and preserve redacted failure diagnostics through resume: [PR #32](https://github.com/proteus-evolve/Proteus/pull/32). |
| Planned | Extend recovery support across adapters with capability checks and integration tests for interruption, rollback, and resume. |
| Planned | Maintain a tested matrix of harness versions, models, and runtime environments; investigate upstream canary failures and validate supported upgrades. |
| Planned | Pin image digests, harness revisions, and complete run configuration in a portable reproducibility manifest, with a one-command reproduction workflow. |

The episode lifecycle and adapter contract are documented in
[`docs/EPISODE.md`](docs/EPISODE.md) and [`docs/ADAPTERS.md`](docs/ADAPTERS.md).

## Developer experience

**Goal:** make it straightforward to start a real run, diagnose a problem, and add an extension.

| Status | Work |
|---|---|
| Completed | Offline quick start, adapter and benchmark contracts, extension templates, scaffolding, and CI conformance checks. |
| Completed | Guides for adding a harness, benchmark, or measurement; runtime recipes and environment build configuration. |
| Planned | A unified run configuration file covering harness, model, goals, evaluators, phases, budgets, and environment settings. |
| Planned | An environment diagnostic command that checks dependencies, credentials, image availability, and adapter readiness, with actionable failure messages. |
| Planned | Publish versioned, tested prebuilt images and document supported platforms, resource needs, and build-cache behavior. |
| Planned | Expand end-to-end recipes for real harnesses, covering first run, evaluation, interruption, repair, and continuation. |
| Planned | Keep installation instructions, examples, compatibility information, and release notes aligned with each release. |

Start with [`CONTRIBUTING.md`](CONTRIBUTING.md),
[`docs/RECIPES.md`](docs/RECIPES.md), and [`environments/README.md`](environments/README.md).
Extension templates live in [`proteus/examples/`](proteus/examples/).

## Measurement and visualization

**Goal:** show what changed, whether it helped, and how much the evolution cost.

| Status | Work |
|---|---|
| Completed | Structural measurements over editable surfaces, behavioural trace distances, reliability analysis, and crystallization/swap measurements. |
| Completed | `proteus watch` and generated reports for run progress, surface growth, and evaluator scores; snapshot-chain export through `proteus repo export` / `push`. |
| Planned | Per-phase and per-episode tool calls, token usage, elapsed time, and cost accounting, recording evolution and evaluation expenses separately. |
| Planned | `proteus compare`: compare runs and experimental arms by episode, cumulative tool calls, or cost, with effect sizes and uncertainty estimates where supported. |
| Planned | Reusable episode detail views showing code changes, validation evidence, acceptance or rollback, failure reasons, and recovery history. |
| Planned | Generalize the website's evolution replay and trajectory views into a reusable viewer for any run or sweep, including looping replay. |
| Planned | Shareable, redacted trace and report exports with explicit control over which evaluation results and artifacts are public. |

Measurement extensions are documented in [`docs/MEASUREMENTS.md`](docs/MEASUREMENTS.md).

## Ecosystem and integrations

**Goal:** support more real harnesses, evaluation tasks, models, and execution environments.

| Status | Work |
|---|---|
| Completed | Source-evolving DeepSeek Harness, Pi, and Codex CLI adapters; offline `minimal` and model-backed `llm` reference harnesses. The Aki adapter requires its separate research checkout. |
| Completed | Local/polyglot tasks and HumanEval and MBPP benchmark integrations. |
| Planned | Candidate harness integrations: Hermes Agent, SWE-agent, OpenClaw, OpenHands, OpenCode, and Goose. Each needs a declared editable surface model and a tested episode lifecycle. |
| Planned | Qualify the existing SWE-bench bridge for supported Lite/Verified subsets, with pinned dependencies, verified grading isolation, and infrastructure failures distinguished from measured task failures. |
| Planned | Additional lightweight, offline-gradable benchmark packs, including suitable BigCodeBench and LiveCodeBench subsets. |
| Planned | General-purpose third-party agent benchmark integrations, including evaluation at selected snapshots, validated independently of research campaign machinery. |
| Planned | Broaden tested model/provider and execution-environment combinations; give each integration a reproducible recipe and conformance coverage. |

Use [`docs/ADAPTERS.md`](docs/ADAPTERS.md) and
[`docs/BENCHMARKS.md`](docs/BENCHMARKS.md) to propose an integration. Describe its editable
surfaces, execution boundary, native trace format, or grading contract in an issue before
starting a substantial implementation.

## Releases and research graduation

Releases collect mature, tested contributions from all four directions. Update statuses
as implementations merge and describe package availability in the release notes.

Research-derived features graduate through separate validation, API review, tests, and
documentation. Experimental self-evaluator modules and LENS-specific integrations remain
subject to that review once the research is stable; they are not committed release items.
