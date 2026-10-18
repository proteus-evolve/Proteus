# Parallel formal evaluation

Set `execution.max_workers` above one to run independent trials concurrently in
`services.evaluate`. The default remains one. The same evaluation service owns
the complete declared cohort, task order, seeds, candidate binding, scoring,
private test records and final receipt. Repeated selected task IDs remain
separate occurrences and each counts toward the evaluation budget.

This is separate from [parallel stages](V1_PARALLEL.md). It enables concurrent
subject execution inside methods such as [Combee GEPA](V1_COMBEE_GEPA.md) without
changing their parent selection or candidate acceptance code. Per-sample
adaptive ACE loops, dynamic batch sizing and persistent CORAL workers remain
separate work.

## Configuration and capabilities

```json
{
  "execution": {"max_workers": 4},
  "budget": {"max_concurrency": 4},
  "actors": {"subject": {"budget": {"max_concurrency": 4}}}
}
```

The fragment illustrates fields to edit in an existing complete recipe. Actual
width is bounded by the requested worker count, run concurrency and the subject
actor's role concurrency. Setting the worker count alone does not override a
budget cap. Calls within one trial retain their own stage scope and ordered
call numbers; different trials do not contend for the same call identity.

Preflight requires independent state, a journal-enabled method, a subject with
`subject.parallel` and an environment with `environment.parallel`. A managed
subject also requires a fresh actor with `actor.parallel`. Bundled local Python,
prompt and managed-code subjects and the independent Harbor paths declare these
capabilities. Third-party implementations must explicitly opt in after checking
their own isolation and concurrency behavior.

Sequence and worker state are rejected with a width above one. The explicit
`open_evaluation` / `step` interface remains serial so methods can receive
feedback and update a candidate or state before the next task.

## Execution, feedback and accounting

Each branch has its own runtime instance, values and call records. All branches
share the durable ledger and the original cohort reservation. Original task
positions determine seeds and trial identities. Source builds are serialized
through the complete build/cache-receipt transaction, preventing concurrent
cache writers while allowing independently built trials to execute together.

An after-prediction grader runs only after that trial's prediction file has been
committed. After-batch and hidden grades remain in finalization. Hidden results
never enter public values or optimization history. The owner joins observations,
calls, build receipts and backend evidence in original task order. Valid task
failure remains a task result; unavailable results are not imputed as zero.

`proteus.eval.map@1` records the cohort operation, ordered rollout IDs, requested
width and measured map duration. An interrupted map cannot claim a continuous
wall-clock measurement. Provider usage and unknown costs keep their existing
ledger semantics. A larger worker count does not multiply the reserved number
of evaluation tasks.

## Recovery and inspection

The owner journal remains the only writer of the full method state. Branch
requests and results are immutable receipts under `private/evaluation-maps`.
They are checked against the original task/candidate/seed, prediction files,
typed values and settled subject, actor, grader and build records before joining.

Recovery first reconstructs or reconciles trials that were actually dispatched.
Only after they are resolved may a reserved request or new trial run. A request
file alone does not prove external execution. Uncertain actor/build calls and
subjects without a reconciliation capability stop dispatch. Previously completed
trials are reused, including when the controller exited before their branch or
cohort commit. `proteus recipe recovery` reports branch positions and states
without exposing task references, model prompts or hidden grades.

The offline Combee GEPA example already configures run and actor limits of three.
Changing its `execution.max_workers` to three exercises this path through the
usual lock/run/resume CLI. It remains an explicitly model-free wiring example;
quality and speedup require real experiments with their declared budgets.
