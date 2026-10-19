# CORAL cooperation reports

`python -m proteus.methods.coral.report RUN --out report.json` reads a completed
`coral.single_island` run without executing a worker, method, model or evaluator.
The output must be outside the source run and must not already exist. A run lease
prevents reporting during controller execution. No optional optimizer or provider
SDK is required. This method-specific tool is internal; it does not add a new
stable `proteus.api.v1` contract.

The report binds the manifest and latest execution snapshot to saved candidate
bytes, formal evaluation receipts, settled ledger entries, worker launches and
completions, gateway closure, knowledge revisions and event decisions. It rejects
missing, inconsistent and unsettled evidence. Content hashes provide consistency
checks, not independent signatures or proof against coordinated rewriting of an
entire run. It does not independently execute a task grader.

## What the report measures

| Output | Interpretation |
| --- | --- |
| `workers`, `generations` | Per-worker budget, generation count, continuity, restart count, status and settled usage. `generation_usage` includes tokens from verified worker completion receipts. `generation_actual` counts only worker generation entries; `actual` also includes that worker’s formal evaluations. Generation charges are summed once; provider cumulative session counters are shown separately. Unknown usage remains `null`. |
| `submissions` | Original queue order, candidate identity and formal result; queued submissions cancelled at shutdown are explicitly distinguished from processed work. |
| `events` | Persisted observations, proposed actions and dispatched interventions, tied to exact worker generations and prompts. Proposed actions at a stop boundary may remain undispatched. |
| `knowledge.revisions` | Published notes, skills and formal attempts, including revision history and declared source references. |
| `knowledge.declared_sources` | A cross-worker edge means a write declared another worker's revision as a source. This is provenance, not a causal benefit estimate. |
| `knowledge.snapshot_captures` | Revisions available in an operation's captured snapshot. The current store does not save a receipt for every knowledge GET; availability does not establish file delivery or use. |
| `timing` | Durable controller intervals and uninterrupted run duration, with explicit missing coverage after a hard exit. This does not measure workers that remain alive while the controller is stopped. |
| `accounting` | Formal evaluations, model calls, actual costs and budget charges, including role/stage attribution from the common ledger. Unknown cost is not zero. |
| `final_cohorts`, `baseline_cohorts` | Saved final confirmations and initial seed evaluations, bound to selected candidates. Private final scores are hidden unless `--include-private` is supplied. |

Worker public reports expose accounting and structural provenance, but do not
include raw prompts, session IDs, logs, environment variables or gateway tokens.
Private evidence hashes and hidden grades are omitted by default. An explicitly
private export includes private scores and evidence hashes; keep it with the
experiment's private records.

`wall_seconds` adds recorded operation durations and can double-count overlapping
work in elapsed-time terms. `ledger_span_seconds` includes gaps and recovery
downtime. Event clock spans cover only recorded observations inside each
controller invocation. None of these is labelled complete active experiment wall
time. CORAL circle-packing final confirmation repeats the same optimization
problem; it is not a held-out generalization task.

## Single- and multi-worker studies

Use the [common StudySpec](V1_STUDY_COMPARISON.md), with a case for each declared
condition/seed and `evaluation_ids` naming the final confirmation:

```sh
python -m proteus.methods.coral.report study.json --study \
  --include-private --out coral-comparison.json
```

The comparison preserves the common protocol, source/lock, seed-coverage and
configuration checks, quality/cost metrics and paired comparisons. It adds worker
counts, generations, restarts, interventions, knowledge publications and declared
cross-worker sources. All compared CORAL evaluations must be the recipe's final
confirmations. Each condition must keep its worker count constant across seeds.

Use `controlled` for a declared ablation and list **all** varied configuration
fields. Use `reference_profiles` for different published/adapted settings; it
produces descriptive summaries without paired baseline claims. Equal submission
allowances do not establish equal monetary cost. The offline one/two-worker
fixtures have different evaluation budgets (7 and 9) and are API checks, not
measurements of collaboration gains.

Tests execute independent worker processes, verify public/private output,
read-only operation, native/fresh continuations, liveness restarts, pending
cancellation, unchanged counting after resume, single/multi-worker comparisons
and rejection of corrupted worker, ledger, gateway, knowledge and event records.
Installed wheel/sdist checks run the actual CLI outside the checkout. Five-family
real-model studies and the full I14/R05 release acceptance remain outstanding.

For saved original-task geometry and complete controller/container acceptance, see [CORAL container study and domain regrade](V1_CORAL_CONTAINER_STUDY.md).
