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

The built-in HTTP chat actor preserves reported token counts even when a complete
JSON response has unusable content, unsupported tool calls, or invalid response
metadata. These calls remain failed, with their known usage settled before a
stage retry. Each nonnegative integral token total is validated independently;
missing or invalid counts remain unknown, and cache/reasoning subsets are not
added again. Provider cost extensions do not establish monetary cost. A transport
or JSON-decoding failure without usable evidence keeps usage unknown and retains
its call reservation. Recovery reuses a settled failure rather than issuing that
attempt again, including after a process exit between settlement and the stage
journal commit.

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
printing prompts, task references or grades. Harbor subjects can now reconcile a
completed worker as described below. Other backends without a reconciliation
capability, uncertain builds, and model responses lost before any durable receipt
remain pending. There is no blanket command that refunds budget or assumes the old
process has stopped.
Known operation failures retain their existing failure policy and are replayed as
failures; resume does not grant additional retries beyond the configured policy.

Public `execution-*.json` files for journal-enabled methods are cumulative snapshots;
do not sum them as separate attempts. The ledger is authoritative for charged usage.
The journal stores full snapshots and favors auditability over storage efficiency;
compaction and parallel operation scheduling are later work.

## Reconcile an independently completed Harbor worker

P04e adds `subject.reconcile` and a `SubjectRecoveryContext`. When the ledger has a
started subject without a settled result, the evaluation service may call the
runner's `reconcile(context)` instead of `run(context)`. The context names the exact
candidate, task, state, seed, execution and private trial directory; it supplies no
execution lease. A reconciler must return the original completed `SubjectResult`
or raise `RecoveryRequired`. It must not submit another build, model call or trial.
This capability currently supports self-contained subjects; managed actor recovery
continues to use settled actor results rather than this hook.

The Harbor worker holds an OS file lock for its lifetime and writes a durable
completion receipt after its trial and exact-label Docker cleanup. That receipt
binds the original request, response and cleanup by content hash. It survives a
hard exit of the controller. Resume reads it only after obtaining the worker lock:
an active worker is left running, with its original budget reservation intact.
Missing, corrupt or mismatched receipts also leave the operation uncertain. A PID
or a response file alone does not establish completion.

Reconciliation checks the candidate, task, seed, state, pinned runtime, source-build
receipt, image and execution parameters against the resumed experiment. It uses the
same result decoder as a normal dispatch, then the evaluation service validates the
result's runtime, candidate and private verifier binding. An audit record under
`evaluations/*/private/reconciled/` identifies the ledger entry and result hash.
Original subject, verifier and build usage are restored and settled once. A worker
that completed with an error yields an infrastructure failure, not a zero score or
an implicit retry. An interrupted reconciliation can itself be resumed.

If the worker was killed before publishing completion, resume still stops. Automatic
adoption of arbitrary Docker state, cleanup of an unconfirmed live worker and
resubmission of an uncertain build are outside this implementation. The generic
inspector remains read-only. These development receipt changes require a new run
under the matching dependency lock; old runs retain their original implementation.

## Acceptance

Tests use hard process exits at candidate, actor settlement, prediction, grader,
evaluation and final-result boundaries. They check actual invocation counts, unchanged
budget, repeated resume, checkpoint schema/integrity checks, RNG/state restoration and
hidden-feedback privacy. The real Docker CI gate also exits after a source-built
Harbor subject settles, then resumes A/B/A: exactly three trials, original verifier
isolation, no repeated initial build, and one copy of each role's usage.
It additionally hard-exits the controller while a Harbor worker holds its lease:
resume first reports the live worker without changing the ledger; the original
worker then finishes independently and a later resume consumes its receipt without
another trial. Offline cases reject changed evidence/identities and exercise a
second hard exit between reconciliation and settlement.
