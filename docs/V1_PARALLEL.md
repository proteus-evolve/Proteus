# Bounded stage maps (P08a)

Methods can now submit independent stage jobs to `services.map_stages`. Each
branch receives typed immutable inputs and its own stage/actor instances. The
method, shared output publication and operation journal remain on one thread.
Actor requests run concurrently, using the same durable run and role budgets.
This is the execution layer for the upcoming Combee recipes; it does not yet
implement augmented shuffle, method-specific aggregation or dynamic profiling.

```python
from proteus.core.parallel import StageJob

jobs = tuple(StageJob(key=str(i), stage="reflect", inputs={"request": ref})
             for i, ref in enumerate(reflection_requests))
mapped = services.map_stages(
    jobs, operation_id="iteration-2/reflections", max_workers=4,
    failure_policy="collect",
)
if mapped.succeeded:
    # Results retain jobs' declared order, regardless of completion order.
    refs = [result.outputs["reflection"] for result in mapped.results]
    # The method builds the reducer's typed input from these refs and submits
    # a normal stage. Further map/reduce levels can reuse the same services.
```

Jobs need unique keys within a named map operation. The map plan binds ordered
inputs, stage IDs, concurrency, failure policy, run, configuration and dependency
lock. Each branch gets a stable operation ID derived from the map ID and job key.
Separate maps must use distinct operation IDs.

## Capabilities and limits

- Methods using maps must enable `run.journal`. Each stage must declare both
  `stage.parallel` and `stage.replay`. These are promises by trusted plugin code:
  independent instances, no shared mutable method state, and repeatable stage
  computation around managed calls. Direct provider calls do not gain recovery.
- Actors must use fresh sessions and declare `actor.parallel`. Built-in echo and
  chat actors support this profile. Runtime instances and workspaces are separate
  per branch; sessions are not silently shared. Coding workspace stages can use
  the map API with a separately validated parallel actor, but the bundled coding
  harness actors do not yet advertise that capability.
- The scheduler limits concurrent branches by the requested `max_workers`, the
  run's `budget.max_concurrency`, and each actor role's concurrency cap. Defaults
  remain one; configure both run and role caps to obtain overlap. A branch's
  internal actor calls are serial, including retries. There is no nested map.
- Actual actor calls reserve model-call and concurrency budget atomically in the
  shared SQLite ledger. Stage attempt costs remain attached to their branch.
  Finite cost/time caps still require backend bounds and remain unsupported;
  unknown provider usage remains unknown. Configure runtime transport timeouts.
- The current map runs stages, not evaluation sessions or persistent workers.
  Subject rollout concurrency and CORAL workers remain separate work.

Stage inputs and outputs are validated before joining. Readable evidence views
remain bound to the receiving stage and supplied input ports. Branches can read
their authorized view through `StageContext`; they cannot call method-owned
candidate, evaluation, search or journal services through the map API. Python
plugins remain trusted code, not a process security boundary.

## Failures, cancellation and recovery

`collect` finishes every independent branch and returns all outcomes.
`fail_fast` stops submitting new branches after observing a failed branch. Work
already submitted drains and settles normally; it is not killed or classified
as free cancellation. The result separately lists jobs never submitted.
Stage retry policy still applies, with each attempt recorded and charged.

Each submitted branch has an immutable request and completion receipt under
`private/maps/`. Actor settlements commit independently of the branch receipt.
The join imports branch requests, results, typed outputs and actor usage in input
order and saves one terminal journal operation. Completion order can affect
which concurrent request obtains the final available budget; the ledger records
that actual order. It cannot increase the cap.

On a process restart:

1. Completed branches are verified against their requests and actor settlements.
2. A submitted branch with settled calls can replay its pure stage computation
   and reuse those calls. A request reserved before dispatch can still run.
3. Any started actor without a settled receipt prevents further map dispatch.
   Its reservation and concurrency slot remain held. This layer does not invent
   external completion or retry uncertain calls.
4. Completed branches are joined exactly once. A completed map or run replays
   without new actor work. `recipe recovery` includes pending branch
   states and worker IDs without exposing prompts or returned values.

`StageMapResult.wall_seconds` measures an uninterrupted map, including queuing
and joining. Actor wall time in the ledger is accumulated work and can exceed
elapsed map time. A map interrupted before its join reports `resumed=true` and
`wall_seconds=null`; its partial timing cannot serve as a profiling measurement.
If the complete join was saved before the crash, the original measurement is
retained. No speedup is inferred from fixture timings.

## Installed example

The included example maps three observations and passes the ordered responses
to a reducer stage. Its echo actor makes no model calls and is not a Combee
reproduction. These commands work from a source checkout; when running elsewhere,
set `--source proteus=/path/to/clean/checkout` for the matching source version.

```sh
python -m proteus.examples.parallel_stages --out /tmp/parallel-study
proteus recipe lock /tmp/parallel-study/recipe.json \
  --plugin proteus.examples.parallel_stages:register --source proteus=. \
  --out /tmp/parallel-study/lock.json
proteus recipe run /tmp/parallel-study/recipe.json \
  --plugin proteus.examples.parallel_stages:register \
  --lock /tmp/parallel-study/lock.json --out /tmp/parallel-study/run
proteus recipe resume /tmp/parallel-study/recipe.json \
  --plugin proteus.examples.parallel_stages:register \
  --lock /tmp/parallel-study/lock.json --out /tmp/parallel-study/run
```

The fixture and HTTP transport tests check actual simultaneous requests, role/run
caps, shared spend, ordered reduction, stage retries and fail-fast draining.
Hard-exit tests cover branch request, actor settlement, branch receipt, join,
map journal and method result boundaries, including two live branches and an
uncertain sibling. Wheel and sdist checks run the example outside the checkout.
