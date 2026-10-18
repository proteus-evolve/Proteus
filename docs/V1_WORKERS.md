# Persistent worker lifecycle (P09b)

A persistent research worker has its own workspace, identity, runtime and budget.
Its process can keep running while the method orchestrator is stopped. This is a
different lifecycle from a bounded `ActorRuntime.execute` call in one stage.

Recipes declare `WorkerSpec(id, role, runtime, budget)`. Worker runtimes are an
explicit plugin kind, included in dependency locks. A runtime implements
`reservation`, `start`, `poll` and `control`; it declares lifecycle and recovery
capabilities. Methods use `services.worker(id)`. Stages do not acquire worker
control merely by sharing the same actor role.

## Generations and session continuity

`start(operation_id, prompt)` returns a `WorkerRef`. A subsequent generation must
name its exact predecessor. The predecessor must have a terminal receipt before
its workspace can be reused. A new generation passes the preceding native session
ID to the runtime; the runtime must confirm that same ID in `resumed_from`.
Silently starting a fresh session produces a failed receipt.

If the session is unavailable, the method can explicitly supply both
`fallback_reason` and `fresh_orientation`. The launch records `fresh_fallback`,
the preceding generation and the reason. It never describes that operation as
continuing the old conversation. An explicit fallback can also discard a known
but unusable session after a runtime failure.

`control(ref, "pause")` requests cooperative interruption so the runtime can save
its session. `control(ref, "stop")` requests termination. Both are asynchronous:
poll until the terminal receipt arrives. A stop may supersede a pending pause.
Signals escalate after the configured grace period; forced termination may yield
no resumable session. Control always names the current generation, so a delayed
request cannot stop a later worker session.

`result(ref)` publishes a typed completion value. The method journal records
launches and control requests. Polling observes current state rather than
replaying an old "active" response indefinitely. The runner refuses to commit a
terminal method result while a declared worker is active or uncertain, including
when the method itself raises an exception. A later resume can finish that work.

## Durable dispatch and accounting

The controller commits an immutable launch and reserves the run, role and worker
budget scopes before dispatch. Each generation consumes one shared concurrency
slot. A supervisor inherits an OS file lease continuously across process startup;
another controller cannot launch a second copy during that interval. Repeating an
operation returns its original generation. Retrying an older completed operation
does not rewind the worker head. An abandoned request that never obtained budget
cannot be launched after another generation has superseded it.

The supervisor writes a dispatch marker before starting the trusted runtime. Its
completion receipt binds the launch, stdout/stderr hashes, exit status, reported
usage and session identity. Controller exits before dispatch can be retried;
controller exits after dispatch reconnect to the existing lease or receipt.
Settling the same receipt is idempotent. Restore verifies historical generations,
including outputs from generations preceding the current head.

A dispatched generation with no live supervisor lease and no final receipt is
`uncertain`. Its budget reservation remains in place. The controller neither
signals a PID read from an old file nor launches another copy. Supervisor loss,
power loss and unverified external provider effects still need backend-specific
reconciliation; no generic exactly-once external execution guarantee is claimed.

## Command bridge

`proteus.worker.command@1` is a POSIX local runtime for explicitly trusted bridge
programs. It uses a standalone Python supervisor and does not require the source
checkout on `PYTHONPATH`. Its executable has an absolute path and a content pin;
the bridge script and other entry files should also be listed in `command_files`.
Both the controller and supervisor verify those pins before execution. A full
runtime dependency closure remains the plugin author's responsibility.

The bridge runs in its worker workspace. It reads one JSON request from stdin
containing the worker identity, operation, prompt, continuity mode, session ID and
fallback reason. The private ledger and knowledge database are not supplied as
protocol inputs. Only named credential environment variables plus basic process
environment are inherited. Credential values are not serialized into receipts.

Ordinary stdout progress lines are retained. At completion, emit one JSON line:

```json
{"event":"result","session_id":"native-session-id","resumed_from":null,"usage":{"model_calls":3,"input_tokens":1200,"output_tokens":400}}
```

On native continuation, `resumed_from` must be the requested session ID. Unknown
usage fields are omitted or null. Duplicate final results and invalid usage are
rejected. The supervisor measures wall time and enforces a generation timeout,
including when the bridge does not read stdin. Pause/stop first sends SIGINT, then
SIGTERM and SIGKILL after successive grace periods.

A bridge can emit an early `session` event with exactly `event`, `session_id` and
`resumed_from`. This confirms identity before a forced stop; it is not a final
result and does not imply known usage. Repeated confirmations must agree. A
`protocol_error` event rejects the generation and its session claims. A final
result must agree with the early identity.

Providers that report cumulative session tokens can return `session_usage` with
`input_tokens` and `output_tokens`. These totals are stored separately from the
generation's `usage` charges. Native launches receive the predecessor's confirmed
totals; fresh/fallback launches receive none. If a generation was interrupted
without confirmed totals, the next delta remains unknown. See
[Codex worker bridge](V1_CODEX_WORKER.md) for a concrete implementation. The new
launch/receipt fields affect content hashes; old locked runs require their
original implementation rather than an implicit migration.

This backend does not enforce hard provider-call or monetary limits, nor a strict
wall-time budget including shutdown. Recipes with those finite caps are rejected;
the generation timeout and shared concurrency cap are separate controls. Other
worker plugins can provide defensible reservation bounds. Missing usage remains
unknown rather than zero. Runtime-reported usage is evidence from trusted bridge
code, not a candidate-controlled claim accepted as an evaluation score.

Local bridge execution is not an OS sandbox. Persistent isolated provider
bridges, the authenticated EvaluationGateway, mediated shared-knowledge access
and CORAL event policies remain subsequent work. This lifecycle implementation
does not by itself constitute a CORAL reproduction.

## Offline recipe and validation

```sh
python -m proteus.examples.worker_lifecycle --out /tmp/worker-study
python -m proteus.cli recipe lock /tmp/worker-study/recipe.json \
  --plugin proteus.examples.worker_lifecycle:register \
  --source proteus=/absolute/path/to/Proteus --out /tmp/worker-lock.json
python -m proteus.cli recipe run /tmp/worker-study/recipe.json \
  --plugin proteus.examples.worker_lifecycle:register \
  --lock /tmp/worker-lock.json --out /tmp/worker-run
python -m proteus.cli recipe resume /tmp/worker-study/recipe.json \
  --plugin proteus.examples.worker_lifecycle:register \
  --lock /tmp/worker-lock.json --out /tmp/worker-run
```

The example uses two real subprocesses and a deterministic bridge with zero model
calls. Worker A pauses and continues its saved session; worker B completes in its
independent workspace. Tests exercise independent controllers racing to start one
generation, hard exits before/after dispatch and journal commits, shared budget
limits, explicit session fallback, unconfirmed native continuation, timeouts,
historical receipt verification and active-worker method recovery. The installed
wheel/sdist gate runs and resumes the recipe through the CLI outside the checkout.

The API/configuration is still experimental. Earlier locked recipes require their
original implementation; this change does not migrate previous run records.
