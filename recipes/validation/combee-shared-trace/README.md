# Common frozen-trace Combee comparison

This **model-free mechanism check** closes the common-input comparison gap without
changing the historical Formula results. It runs the production sequential ACE,
naive-parallel ACE, fixed Combee-ACE and dynamic Combee-ACE methods. No provider is
called and no quality, model latency, speedup or paper-reproduction claim follows.

From a checkout with Proteus installed (`pip install -e '.[dev]'`):

```sh
python recipes/validation/combee-shared-trace/run.py --out /tmp/combee-shared-trace
python recipes/validation/combee-shared-trace/run.py --out /tmp/combee-shared-trace --verify
pytest tests/test_v1_combee_shared_trace.py -q
```

Choose a new output directory for each run. `trace.json` is the immutable common
input: eleven ordered tasks with question/context, reference, exact successful
prediction, exact measured feedback, exact reflection response, seed 0 and the
empty-playbook H0 hash. Hashes of these bytes are checked at every actor call.
The fixture ignores model-conditioned improvements: responses replay the frozen
prediction/reflection. The actual task evaluator computes feedback and the auditor
checks it against the frozen record. Two separate validation task IDs satisfy the
offline protocol; they reuse synthetic inputs and are not a quality split. With
`eval_steps=100`, this eleven-example fixture does not claim selection quality.

The curator is an explicit deterministic test double: it parses the **actual**
curation/reflection or reducer-proposal input and returns ADD operations for the
consumed source IDs. Production methods apply the operations through real update
services. This makes sample consumption, ordering, duplication and lineage
observable; it does not simulate a learned curator or require equal playbooks.

| Arm | Learning batches | Grouping | Curator invocations |
| --- | --- | --- | ---: |
| Sequential ACE | one example at a time | one curation per example | 11 |
| Naive parallel | 3 / 3 / 3 / 2 | single pass, no copies, one group | 4 |
| Fixed Combee | 3 / 3 / 3 / 2 | two copies, two seeded groups, reducer | 12 |
| Dynamic Combee | 1 / 2 / 4 / 4 | same Combee grouping, profiled batch controller | 12 |

Every arm reflects on the same eleven original samples once. Each makes 22 generator
invocations (learning predictions and production post-update reevaluation).
Original sample count is distinct from copied aggregation occurrences. Actor
invocations here are **not** provider calls, all of which are zero.

Dynamic timing replaces only `TimingService`'s clock reference inside this runner.
Each ordinary batch is assigned one declared synthetic second. Real begin/finish
journals and the production controller profile sizes 1, 2, 4, choose size 4, then
consume the final four remaining samples under that selected schedule. These
synthetic observations validate scheduling; actual host wall time is incidental.

`comparison.json` and each arm's `audit.json` bind the source commit/runtime tree,
runner/trace bytes, resolved configuration, actual sample/reflection payloads,
original/copy group plans, request/response receipts, candidate lineage and final
playbook, dynamic decisions, raw execution and manifest hashes. Raw run directories
retain the records and artifacts. `--verify` rereads them without executing work;
changed source/configuration/lock identities, trace bytes, execution, saved provenance or independent invocation receipts
are rejected. A completed-resume check confirms zero additional actor calls; this
is not an interrupted-recovery experiment. Validation failures from fixture setup
are retained in the release audit, separate from accepted runs.

Execution requires a clean committed checkout. Readback binds each saved manifest
to its external recipe/lock and to the reported source commit, and verifies that
commit's runtime tree and fixture blobs. A later documentation-only HEAD can read
an unchanged reader's older report; HEAD equality is not required.
