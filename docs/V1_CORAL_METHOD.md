# CORAL single-island controller

`coral.single_island@1` composes the existing worker, shared-knowledge,
evaluation-gateway, event-policy and final-selection interfaces into one
checkpointed research loop. Workers own editable workspaces and choose when to
submit candidates. The controller evaluates immutable submissions and delivers
policy interventions using each worker's declared session protocol.

The current runnable study uses long-lived deterministic subprocesses. It
demonstrates the orchestration and recovery mechanisms without model calls. A
production agent bridge, an original non-training task, and real single-worker
versus multi-worker results remain required for R05 acceptance.

## Components and control flow

1. Evaluate the initial candidate on the declared observed formal protocol.
   Seed each independent `candidate/` directory from that immutable artifact.
2. Start the declared workers. Shared notes, skills and formal attempts remain
   available through their authenticated gateway connections.
3. Claim an admitted submission in FIFO order. Freeze/evaluate its exact bytes,
   publish the permitted evidence and shared attempt, and observe its formal
   result through the selected event policy.
4. Apply the policy's combined actions with a header identifying that worker's
   formal result. An intervention pauses the original generation, waits for its
   terminal receipt, and starts a native or explicitly fresh continuation.
5. Tick the policy and record liveness observations. A confirmed dead worker may
   restart with its own latest feedback, up to the configured restart bound.
   Uncertain workers require reconciliation and retain their reservations.
6. At the submission, poll or restart limit, atomically close admissions. Cancel
   only queued, undispatched submissions; claimed evaluations require recovery.
   Stop all current workers and wait for confirmed completion receipts.
7. Select from comparable observed formal evaluations, including the baseline.
   Optionally evaluate selected candidates on a separate hidden test. Capture the
   final shared-knowledge snapshot and publish the typed `coral.summary@1`.

The last allowed formal result is recorded before shutdown, but no new
intervention is dispatched for it. This follows the reference manager's ordering
of automatic stopping before heartbeat dispatch.

## Run the offline study

From an installed package, run these commands in an empty working directory:

```sh
python -m proteus.examples.coral_search --out study --workers 2
proteus recipe lock study/recipe.json \
  --plugin proteus.examples.coral_search:register \
  --source proteus=/absolute/path/to/Proteus --out study/lock.json
proteus recipe run study/recipe.json \
  --plugin proteus.examples.coral_search:register --lock study/lock.json \
  --artifact-store study/artifact-store --out study/run
proteus recipe resume study/recipe.json \
  --plugin proteus.examples.coral_search:register --lock study/lock.json \
  --out study/run
```

Use `--workers 1` in a different output directory for the single-worker condition.
The source pin identifies the checkout used to build the installed package.
Neither condition is a research quality or speed comparison: its workers follow
deterministic fixture logic and have known zero model usage.

Worker A shares a factor through a note, first submits the original behavior,
then changes its code after an intervention. Worker B reads the note, uses its
contents to construct a successful candidate, and publishes a skill citing both
the note and its own formal attempt. Both initial workspaces come from the same
seed; subsequent edits survive continuation. With two workers there are three
formal submissions, two interventions, nine task evaluations including baseline
and hidden final test, and no active worker reservations on completion.

## Recipe interfaces

The production method and policy can be registered separately from the example:

```text
--plugin proteus.methods.coral.method:register
--plugin proteus.methods.coral.policy:register
```

| Configuration | Meaning |
| --- | --- |
| `method.options.gateway` | Gateway containing exactly the method's worker set |
| `method.options.event_policy` | EventPolicy attached to that gateway |
| `bootstrap_prompt`, `worker_prompts` | Shared orientation and optional per-worker overrides |
| `max_submissions` | Maximum processed submission results, including failed results |
| `max_polls`, `poll_seconds` | Bounded claim-loop iterations and per-claim wait |
| `max_restarts_per_worker` | Bound on liveness-driven restarts; heartbeat generations are separate |
| `stop_timeout_seconds` | Time per controller invocation to confirm all workers have stopped |
| `minimize`, `final_selector` | Objective direction and a declared final-selection policy |
| `final_evaluation` | Optional separate hidden test protocol |
| `event_policies[].resume_policy` | `native` or `fresh`; fresh requires explicit orientation |
| `gateways[].knowledge_access` | Must be `read_write` for this method |

There must be one comparable, selection-eligible formal evaluation protocol.
Task subsets with different protocols must not be silently mixed into a scalar
final ranking. The current autonomous-file method requires an input-free subject
protocol. The gateway only accepts evaluations explicitly offered to workers;
the final hidden test is never offered to them.

Worker runtimes, tasks, candidate execution, evaluation, event policies and final
selection are independently registered components. Switching a worker backend
does not require a separate CORAL loop. The first command runtime is a trusted
local bridge, and its loopback transport does not establish an OS sandbox or
support workers inside arbitrary remote containers. Candidate execution can use
the existing Harbor backend independently.

## Recovery and shutdown

Worker seeding has a durable intent, staging copy, atomic rename and immutable
receipt. Recovery can rebuild its own partial staging copy before any launch.
It verifies a renamed but unpublished destination against the artifact. Once a
worker has launched, replay never resets the editable working copy.

Admission closure and request acceptance share the gateway transaction boundary.
A previously accepted operation can still be retried after closure. New uploads
are rejected even if they passed preflight immediately before closure. The close
receipt retains the original pending set; cancellation neither fabricates an
evaluation nor charges a task. A queued cancellation cannot subsequently be
executed through `process` because processing also requires an actual claim.

Liveness observations, event inputs, intervention context, worker generations and
stop decisions are recorded. Checkpoints retain the processed-result list,
per-worker feedback, restart counts and controller phase. After a hard exit,
replay reconnects to existing workers and gateway receipts. It does not issue a
second evaluation, replace native sessions with fresh ones implicitly, or repeat
settled charges. Unknown external completion remains a recovery requirement.

Gateway queue schema 2 adds the immutable closure record. Schema 1 runs must be
resumed with their original implementation; this change does not silently migrate
old stores or alter their dependency locks.

## Reference profile and remaining differences

The source reference is CORAL commit
`0123dfb939b35228cf2c1fde224cd0561e727408`. Its unmodified heartbeat implementation
and licensed prompts are packaged with the separate
[event-policy adapter](V1_EVENT_POLICIES.md). The fixture uses adapted prompts
that refer to Proteus's mediated APIs.

- The reference manager coalesces scored observations to the latest available
  attempt. This controller serially processes each formal result. Worker
  exploration is concurrent, while formal controller dispatch is serial.
- A submission cap, poll cap and fixed restart bound are explicit Proteus recipe
  options. The reference's crash classification, restart cooldown and output
  stall watchdog are not reproduced by this controller.
- The baseline and optional hidden final test use Proteus's common comparison
  protocol. They consume the declared budget and are not worker submissions.
- `reference_manager` recreates the upstream heartbeat runner lifecycle;
  `persistent_cooldown` is an explicit extension. These profiles cannot be
  presented as equivalent.
- Multi-island coordination, migration, model training, production provider
  sessions and real benchmark results are outside this incremental delivery.

## Verification

`tests/test_v1_coral_method.py` exercises one and two actual worker subprocesses,
knowledge-derived candidate improvements, native/fresh interventions, bounded
restart and idle shutdown, queued cancellation, final selection and hidden tests.
Hard exits cover baseline settlement, checkpoint publication, formal evaluation,
intervention, liveness observation, admission closure, shutdown, final selection
and hidden-test settlement. Recovery checks compare task counts, worker
generations and the durable ledger, including a second no-op resume.

`tests/test_v1_gateway_closure.py` additionally covers the upload/close race and
refusal to cancel claimed work. `tests/test_v1_worker_seed.py` covers four seed
publication crash boundaries and preservation of worker edits. The installed
acceptance script runs the public CLI outside the source checkout for both wheel
and sdist.

For completed runs, [CORAL cooperation reports](V1_CORAL_REPORT.md) validate and summarize worker generations, formal submissions, knowledge provenance and interventions, and compare explicit single/multi-worker studies.
