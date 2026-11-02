---
name: proteus-use
description: Configure, run, resume and analyze self-improvement experiments with the proteus-evolve/Proteus framework. Use when the user wants to choose or compose a Proteus method, bind optimizer/subject harnesses, set up visible/hidden/self evaluation, reproduce a recipe, inspect results or visualize a Native sweep. Do not use for unrelated software named Proteus or for implementing framework internals; use proteus-contribute for extensions and code contributions.
---

# Use Proteus

Turn the user's experiment into a runnable, source-locked recipe and an evidence-backed
result. Work within the user's requested scope, existing authorization and budget.
If the request is analysis only, inspect the saved run without starting an experiment.

## Locate the matching installation

1. Find the user's Proteus checkout and Python environment. Inspect `git status`,
   the source revision, `python -m pip show proteus-evolve` and `proteus --help`.
   Keep existing work intact; put run outputs outside the source tree.
2. Resolve documentation paths below against that **checkout root**, not this
   installed skill directory. Skills are distributed in the Git repository's
   `skills/` folder separately from the Python wheel. Keep skills, documentation
   and runtime at the same tag or commit where possible. If only a package is
   installed, obtain its matching source/docs before producing source locks; do
   not substitute the latest branch for the source of an existing run.
3. Read `README.md` and the relevant catalog entry using `proteus recipe catalog`.
   Use the matching recipe README and `proteus recipe <command> --help` for exact
   options. A generic preparation template may still require data and credentials.
   An offline example is a mechanism demonstration, not a model benchmark.

## Specify the experiment

Record a concise setup before execution. Infer choices from the user's goal and
existing files; ask only for consequential information that is missing.

| Choice | What to pin |
| --- | --- |
| Goal and candidate | Task, success criterion, artifact format and allowed mutable paths: prompts, memory, skills or harness code |
| Method | Native, GEPA, ACE, Meta-Harness, Combee or CORAL profile; stages, search policy and stopping rule |
| Harness roles | Optimizer/proposer, subject executing the candidate, optional independent model grader; integration, model and source/image pins for each |
| Evaluation | Tasks and split IDs, feedback visibility, selection eligibility, schedule, metrics and baseline |
| Resources | Pilot size, concurrency, time/call/token/cost limits supported by that backend, artifact location and existing credentials |

Use `docs/V1_HARNESS_ROLES.md` to check capabilities and candidate formats. A harness
integration and an optimization method are separate choices. Native self-evolution
can use the same integration in optimizer and subject roles; another method can
use a separate coding agent. The subject runs a candidate on tasks; it does not
own the method's search loop. Do not claim every method/harness/role combination is
supported just because both names appear in a catalog.

For composition, use implemented combinations such as ACE + Combee or GEPA + Combee,
or explicit stages that the selected method actually invokes. Adding a stage to a
configuration does not make every method execute it. For a missing mechanism,
prepare an extension through the optional `proteus-contribute` skill.

Keep visible feedback available to optimization and hidden references, verifier
traces and final scores outside optimizer inputs and selection. An owner-side
report may include hidden results after selection; do not feed them into the next
proposal. Record any intended visible/hidden overlap. Self-evaluation is separately
typed evidence and must be explicitly consumed by the method; it does not replace
an independent task score or require a new harness on every episode.

## Prepare, check and run

For a first-time installation check, run this offline flow from the matching
Proteus Git checkout after installing it in a virtual environment:

```bash
PROTEUS_EXAMPLE="$(mktemp -d)/comparison"
proteus recipe example candidate-comparison --out "$PROTEUS_EXAMPLE"
proteus recipe check "$PROTEUS_EXAMPLE/recipe.json"
proteus recipe lock "$PROTEUS_EXAMPLE/recipe.json" \
    --source "proteus=$PWD" --out "$PROTEUS_EXAMPLE/lock.json"
proteus recipe run "$PROTEUS_EXAMPLE/recipe.json" \
    --lock "$PROTEUS_EXAMPLE/lock.json" \
    --artifact-store "$PROTEUS_EXAMPLE/artifact-store" --out "$PROTEUS_EXAMPLE/run"
proteus recipe resume "$PROTEUS_EXAMPLE/recipe.json" \
    --lock "$PROTEUS_EXAMPLE/lock.json" --out "$PROTEUS_EXAMPLE/run"
proteus recipe export "$PROTEUS_EXAMPLE/run" --out "$PROTEUS_EXAMPLE/export"
```

For the requested experiment, adapt the relevant recipe rather than this fixed-candidate
fixture. Prepare data, goals and candidate artifacts; register any external plugin
hooks; then check and lock **all declared source repositories**. Retain the generated
recipe, lock, command and configuration hash. `check` validates contracts but does
not prove a model's quality or that a live credential works.

When execution is requested and authorized, start the smallest useful pilot covering
proposal/update, candidate execution, evaluation and consumption of feedback. For
staged source evolution, also verify that the accepted edit is active in the next
episode, not merely present in a candidate directory. Respect an explicitly requested
full run; do not impose an extra approval step once the scope is authorized.
For a planning-only request, provide the ready commands without launching them.
Keep credentials in the existing environment/secret store, never in recipes or reports.

Follow live logs and durable artifacts. Distinguish a model call, source build, trial,
and evaluation from one another. A missing result or infrastructure failure is not
a zero task score. Treat opaque usage or an unenforceable cost cap as unknown, not
as a guarantee of bounded spending; report the actual supported limits.

## Resume without repeating uncertain work

Resume only with the original source, recipe, plugin implementations and lock.
If the source or setup changes, create a new lock and run directory; preserve the
original experiment. A completed resume should reuse committed work.

If `RecoveryRequired` occurs, inspect the existing run with:

```bash
proteus recipe recovery recipe.json --lock lock.json --out RUN
```

Read `docs/V1_RECOVERY.md` for the backend's reconciliation path. Inspect original
receipts, active workers and reservations before taking action. Do not delete a
reservation, assume a timeout means the provider did no work, or redispatch an
uncertain operation. Preserve failures and known/unknown usage in the same ledger.

## Present results and visualization

- Export generic method runs with `proteus recipe export`; use `report.json` and
  the CSV tables, or `docs/V1_STUDY_COMPARISON.md` for a study. Private scores require
  an intentional owner-side `--include-private` export; keep it out of agent feedback.
- For a **Native sweep** with `manifest.json`, use `proteus visualize --out RUN`.
  The local viewer exposes Evolution, Measurements, Configuration and Snapshots.
  It does not currently render every generic method recipe; use exports for those.
- Report the exact source/configuration, task counts, completion state, baseline,
  selected candidate and last candidate separately, with links to evidence. Include
  elapsed time, known usage/cost and unknowns, failures, blockers and the next step.
- Distinguish offline fixture, minimal live pilot, adapted benchmark and matched
  paper reproduction. Do not infer statistical significance from a single seed or
  compare unlike task scores as a ranking. Cite original methods and code.

## Read on demand

All paths are relative to the matching Proteus checkout root.

| Need | Read |
| --- | --- |
| Choose or reproduce a recipe | `docs/V1_RECIPE_CATALOG.md`, `recipes/README.md`, then that recipe's README |
| Roles, source evolution and support | `docs/V1_HARNESS_ROLES.md`, `docs/ADAPTERS.md` |
| Evaluation and Native scheduling | `docs/BENCHMARKS.md`, `docs/EPISODE.md` |
| Recovery and uncertain operations | `docs/V1_RECOVERY.md` |
| Reports and visualization | `docs/V1_RESULT_EXPORT.md`, `docs/V1_STUDY_COMPARISON.md`, `docs/VISUALIZE.md` |
| Original papers, code and deviations | `docs/METHOD_REFERENCES.md`, `references.bib` |
