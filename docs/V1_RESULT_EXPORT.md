# Export saved experiment evidence

```sh
proteus recipe export /path/to/run --out /path/to/new-export
proteus recipe export /path/to/run --out /path/to/private-export --include-private
```

The exporter reads a settled recipe run and writes `report.json` plus eight CSV
files: `tasks.csv`, `cohorts.csv`, `candidates.csv`, `costs.csv`,
`role-stage-costs.csv`, `stage-attempts.csv`, `controller-intervals.csv`, and
`native-usage.csv`. No model, method plugin or grader
is imported or executed. It works with the standard records produced by the five
method families, including third-party value schemas and optional GEPA recipes.
The destination must be new and outside the run; existing experiment files are
not modified. The controller lease prevents exporting while a controller owns the
run. Unsettled external operations require reconciliation before export.

## What the tables mean

- Tasks preserve rollout identity, task occurrence, candidate and artifact,
  evaluation/protocol identity, split, status, score and metric values. Repeated
  samples are separate occurrences. Missing scores remain `null`.
- Cohorts aggregate each saved candidate batch. A mean is unavailable if any
  expected score is missing; failures never silently reduce the denominator.
  Separate evaluation protocols, candidates, rounds and dataset splits are not
  pooled into one headline score.
- Candidates retain parent IDs, artifact hashes, codec and producing operation.
- Costs use each run-ledger entry once, with a breakdown by role, stage and kind.
  They do not sum overlapping stage summaries or budget-scope memberships. Actual
  unknown usage stays `null`, even if budget accounting retained a reservation.
  `budget_charged` is therefore separate from `actual`.
  Actor entries also expose `input_tokens` and `output_tokens` in both cost CSVs.
  `accounting.actor_tokens` aggregates managed actor entries only, including
  failed attempts; non-actor token coverage is not inferred from those totals.
  An empty actor set is unavailable, and one unknown actor keeps its total null.
- Stage attempts show the recorded statuses and actual repeated attempts of the
  same operation. Resuming a completed run does not count as a retry or another
  set of trials. Worker generations remain individually identifiable cost entries.
- Native usage uses distinct adapter-boundary receipts, including incomplete or
  failed attempts. `native_accounting.reported` is separate from the managed
  ledger: an unknown component keeps its total `null`, while `known_subtotal`
  explicitly shows only the measured portion. Its cumulative scope survives
  native resume even though other native results cover the latest invocation.
  See [native usage evidence](V1_NATIVE_INPUTS.md#reported-native-usage).

Generic `proteus.harness.*` and `proteus.workspace.*` actors use the native
adapter's verified usage receipts when available. Those values enter their
existing actor ledger entry once, rather than the separate native-recipe table.
`accounting.native_receipts` binds each actor to the cumulative receipt hash,
boundary count and inference fields. Export and settled-result recovery re-read
the private evidence; missing or changed receipts fail validation. Captured
fresh sessions cannot be counted again by a second actor dispatch. Inputs and
provider/session bytes remain under `actors/.proteus-records/`, outside the
disposable actor workspace; only owner exports include their source hashes.
Deleting the disposable workspace or relocating the whole run does not require
another model call. Legacy/custom adapters without receipts retain their
reported counters, without gaining receipt-backed coverage.

Known token receipts do not establish complete internal-call coverage, prices,
or a safe call bound. Generic finite cost/call limits for these harnesses remain
rejected. Actor wall time covers the wrapper invocation, not the sum of nested
native boundary times. Runtime exceptions may preserve a failed `ActorResult`
through `ActorFailed`; the stage still fails and the ledger retains observed
usage rather than replacing it with unknown merely because an exception occurred.

The latest execution snapshot is used. A journal method restores its cumulative
history; a non-journal method's report covers its latest invocation only. This
scope is explicit in `history_scope`. Native opaque execution may have no managed
ledger or task rows; missing coverage is reported rather than inferred from text.

`wall_seconds` in accounting sums operation durations, which can overlap.
`ledger_span_seconds` is the span between the first and last ledger events and
includes pauses or recovery downtime. The separate `timing` object and
`controller-intervals.csv` contain measured controller intervals; see
[V1_CONTROLLER_TIMING.md](V1_CONTROLLER_TIMING.md) for coverage and interruption
semantics. The dedicated Combee study retains its outer study timer, which starts
before invoking the runner and therefore has a different measurement boundary.

## Integrity and private results

The report binds its manifest, configuration and dependency lock, validates saved
value hashes, candidate lineage, artifact bytes, task/rollout/grade/receipt
relationships and ledger event/settlement consistency. `evidence` records source
paths and content hashes; `source_run` locates the original saved predictions and
configuration. The logical ledger hash covers the read-only transaction snapshot.
The report itself has a content hash.

This is evidence validation and aggregation. It does not rerun arbitrary evaluator
code or establish metric correctness. Original-domain audit reports remain the
place for independent regrading against pinned references and backend proofs.

Hidden grades, their status/metrics and private evidence hashes are omitted by
default. `--include-private` produces an owner-side offline report with those
results. Raw prompts, task references, predictions and artifacts are not copied
into either export. Do not feed the private report back into an optimization run.

Reference profile, paper/code citations and declared implementation deviations
come from the original manifest. Export does not upgrade a fixture into a model
result or label an adapted recipe as a paper reproduction. Cross-run comparison,
multi-seed aggregation and release acceptance remain separate work.

[CORAL cooperation reports](V1_CORAL_REPORT.md) extend these common saved-run and study contracts with per-worker generation usage, queue closure, knowledge provenance and event dispatch evidence.
