# Proteus Roadmap

Proteus measures how an agent harness changes itself. Growing it means growing two axes —
**what can evolve** (harness adapters) and **what gets measured** (benchmarks) — plus the
layer that makes results legible (analysis) and the plumbing that makes runs cheap to
trust and reproduce.

The contributor on-ramp for the first two is already in place: a checked `HarnessAdapter`
contract, a `BenchTask` contract, copy-paste templates under `examples/`, a scaffolder
(`python -m proteus.scaffold`), and a CI conformance gate (`tests/test_conformance.py`).
Start at [`CONTRIBUTING.md`](CONTRIBUTING.md). Items below are tagged
**[good first issue]**, **[medium]**, **[large]**.

## T1 — More harnesses

Each harness is one adapter (seven methods; see `CONTRIBUTING.md`). The framework's thesis
is that the *harness's own source* is the thing that evolves, so the most valuable targets
are ones whose source is tractable to rebuild from an episode's edits and that already
expose named, self-editable surfaces. The framework now provides **staged activation**
(`staged_activation = True` — an edit takes effect the next episode, gated by an optional
`validate_candidate()`) and **framework continuity** (`continuity_mode = "framework"`), so
a from-source coding harness that executes its own edits is well supported. Priority order:

| Priority | Harness | Why | Fit |
|---|---|---|---|
| ★ first | **Hermes Agent** ([NousResearch](https://github.com/NousResearch/hermes-agent)) | Python, no build step (from-source is trivial), and a *built-in self-improvement loop* over named surfaces the agent already edits — skills, persistent memory, config, context files — plus recorded trajectories and session resets. The most on-thesis target available. | very high |
| ★ | **SWE-agent** | pairs directly with the SWE-bench track (T2) | high |
| ○ | **OpenClaw** ([openclaw](https://github.com/openclaw/openclaw)) | TS + `pnpm build` → the same containerized rebuild-from-source shape as the shipped `dsh`/`pi`; rich tools/skills/plugins surfaces (Plugin SDK, ClawHub). Its gateway/daemon means an episode drives one headless session. | medium-high |
| ○ | **Codex CLI** ([openai/codex](https://github.com/openai/codex)) | Rust + Bazel → the same containerized rebuild-from-source shape as `dsh`/`pi`, but a heavier compile per boot; a natural `AGENTS.md` disposition-in-files channel plus `.codex` config. A coding-agent CLI, so it also pairs with the benchmark tracks. | medium |
| ○ | **OpenHands** (ex-OpenDevin) | ships its own runtime/sandbox; large community | medium (heavy) |
| ○ | **OpenCode**, **Goose** | broaden language/loop coverage | medium |

- **[medium]** Add the **Hermes** adapter (recommended first harness): its skills/memory
  surfaces map straight onto `surfaces()`, and no build step keeps `run_episode` simple.
- **[large]** Add a containerized adapter (**OpenClaw** / **Codex CLI** / OpenHands)
  following the `dsh`/`pi` rebuild-from-source + boot pattern, declaring
  `staged_activation = True`. Codex's Rust/Bazel build is the heaviest per-boot cost of the
  three — cache the compiled output on `/state` as `dsh`/`pi` do.
- Proposing another harness? Open an issue describing its editable surfaces and how one
  episode maps onto `run_episode`/`read_trace` before writing code.

## T2 — More benchmarks

Each benchmark is one `BenchTask` (`setup` + `grade`); see `CONTRIBUTING.md`.

- **Shipped:** HumanEval and MBPP lightweight packs.
- **[good first issue]** More lightweight, offline-gradable packs, one `BenchTask` each:
  BigCodeBench (lite), a LiveCodeBench subset.
- **[medium]** SWE-bench is already implemented (`proteus/bench/swe.py`) but heavy
  (x86_64 + large disk). Make it usable: wire **SWE-bench Lite / Verified** subsets, and
  degrade result-shape drift to a legible `0.0` instead of raising.
- **[medium]** Finish routing grading through the episode sandbox. The plumbing exists
  (`proteus/bench/sandbox.py::run_python`, injected via a `grade(..., sandbox=...)`
  parameter) and `local`/`polyglot` already use it; migrate **`swe`** onto it and document
  the `PROTEUS_GRADER_IMAGE` knob. (Grading agent-authored code on the host, outside the
  episode's isolation, was a review finding — keep new benchmarks on the sandbox path.)

## T3 — Output & analysis

Live tracking already exists: `proteus watch` serves a self-contained `report.html`
(per-run progress, per-surface growth curves, evaluator scores), and `web/server.py` is
the hosted playground. The gap is **post-hoc, cross-run analysis** — `measure` /
`reliability` / `audit` emit numbers, with no comparative view.

- **[medium]** `proteus compare`: put several arms/runs side by side — structural and
  behavioural measurements with effect sizes, confidence intervals, and crystallization
  points — as one page or table.
- **[large]** Generalize the one-off "atlas" (browse every episode's evolution path across
  a whole grid) into a reusable view driven by any sweep's snapshot chain.

## T5 — Reproducibility & cost

- **[medium]** Per-episode token / cost accounting, surfaced in the run manifest and the
  tracking page. The manifest already records the model per sweep; extend it to token and
  cost counters — anyone running a grid needs them.
- **[medium]** One-command reproduce: pin the environment image + config so a published
  run can be re-executed, building on `proteus repo export` / `push` (which already turn a
  run's snapshot chain into a normal git history).
