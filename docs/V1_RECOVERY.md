# 1.0 development: method checkpoints and operation recovery

P04d enables `recipe resume` for `proteus.compare`, `proteus.sequence`, and custom
methods declaring both `run.resume` and `run.journal` with a registered state schema.
Native recipes retain their existing episode recovery. Configurations, plugin
implementations, dependency locks and state versions must match the original run;
this does not migrate earlier development runs to a new source version.

## Durable operations and checkpoints

The run's single-writer lease covers a private, immutable operation journal under
`private/recovery/`. Each request is durably published before its operation begins;
its result and service snapshot are published together afterward. Hashes bind each
record to its request and the run manifest. Complete records are never overwritten.

The journal covers candidate creation/deltas, stages, opening an evaluation,
individual predictions and feedback, explicit sequence-state updates, evaluation
finalization, method checkpoints and the final method result. A method checkpoint
contains its versioned payload, archive reference, sampling cursor, framework RNG,
custom RNG data and open evaluation requests. Service snapshots preserve typed
values, candidate/operation records, role usage and private evaluation session
state. Private references and verifier evidence remain outside public Values.

On resume, the executor restores the latest committed method checkpoint. Subsequent
committed operations are replayed in their original order by returning their saved
results and restoring their service state. A changed call order or changed input
fails explicitly. Returning a final result before consuming an existing operation
also fails. A committed final method result repairs public exports without invoking
the method or external services again.

New evaluation operations execute afresh. This recovery mechanism does not share
scores across experiments or reuse an evaluation merely because its candidate is
similar. The same ledger retains every reservation, actual usage, unknown usage
and violation; resume neither creates a new budget nor settles work twice.

## Implement a resumable method

Register `state_schema=YourState` and capabilities `("run.resume", "run.journal")`.
The method receives its most recent `MethodState` as `restored_state`:

```python
payload = YourState.from_dict(restored_state.state.payload) if restored_state else YourState(...)
# Continue from the payload's cursor; manage control flow in the method plugin.
checkpoint = services.checkpoint(
    payload,
    archive=archive_ref,
    sampling_cursor=sample_index,
    random_state={"your_generator": your_generator_state},
)
```

Use `services.rng` for framework sampling; its state is restored automatically.
Other generators must be restored by the method from
`restored_state.random_state["method"]`. Global Python/NumPy RNGs and arbitrary
in-memory plugin objects are not implicitly captured. Keep cursors, IDs and method
decisions in the payload. Checkpointing belongs at method boundaries, outside a
running stage or evaluation call.

An online method can checkpoint an open evaluation. Store its operation ID and
retrieve it with `services.evaluation_session(id)` after restore; `session.position`
reports committed predictions. The session restores explicit JSON state, observation
history, feedback timing and grades. Persistent filesystem or worker sessions are
separate I11 work, not implied by this API.

An interrupted stage may run again only if its plugin declares `stage.replay`:
the plugin promises that rerunning its Python control flow is safe and all external
work goes through `context.actor`. The built-in prompt stage declares this. Settled
actor results are returned from the ledger with their original usage; an unfinished
actor dispatch is never repeated. Completed stages need no re-execution, regardless
of this capability. Arbitrary external effects performed directly by trusted Python
plugins are outside this guarantee.

## Interruption and uncertainty

There is an unavoidable gap between an external side effect and the controller's
durable receipt. Subject and grader results are retained in their ledger settlements
before prediction/grade publication. Recovery can finish that publication using the
original result, including a Harbor trial's private verifier channel and source-build
usage. It does not start another container, build, model call or grader in this case.

If an actor/subject/grader is marked started but has no settled result, resume returns
`RecoveryRequired`. The unfinished operation and budget reservation remain intact.
Use the read-only inspector to locate them:

```bash
proteus recipe recovery recipe.json --lock lock.json --out RUN
proteus recipe resume recipe.json --lock lock.json --out RUN
```

The inspector reports checkpoint/operation cursors and unsettled ledger IDs, without
printing prompts, task references or grades. Backend-specific reconciliation of lost
responses and uncertain builds/trials is still pending; this batch deliberately has
no blanket command that refunds budget or assumes the old process has stopped.
Known operation failures retain their existing failure policy and are replayed as
failures; resume does not grant additional retries beyond the configured policy.

Public `execution-*.json` files for journal-enabled methods are cumulative snapshots;
do not sum them as separate attempts. The ledger is authoritative for charged usage.
The journal stores full snapshots and favors auditability over storage efficiency;
compaction and parallel operation scheduling are later work.

## Acceptance

Tests use hard process exits at candidate, actor settlement, prediction, grader,
evaluation and final-result boundaries. They check actual invocation counts, unchanged
budget, repeated resume, checkpoint schema/integrity checks, RNG/state restoration and
hidden-feedback privacy. The real Docker CI gate also exits after a source-built
Harbor subject settles, then resumes A/B/A: exactly three trials, original verifier
isolation, no repeated initial build, and one copy of each role's usage.
