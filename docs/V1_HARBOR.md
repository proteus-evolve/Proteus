# 1.0 development: Harbor trials and evaluation accounting

P04b adds `proteus.harbor`, `proteus.harbor_subject` and `proteus.harbor_reward`.
They execute pinned file-tree candidates in independent Docker trials with a
separate verifier. This page describes the independent-trial backend. The development line also
provides [explicit sequence memory](V1_HARBOR_CODE.md) and
[persistent Harbor workers](V1_HARBOR_WORKERS.md). See the
[current architecture and method matrix](V1_OVERVIEW.md) for their composition;
unknown external work without a durable receipt is not automatically replayed.

P04c adds candidate-bound source builds and verified, run-local image reuse. See
[V1_SOURCE_BUILDS.md](V1_SOURCE_BUILDS.md) for the build contract and its acceptance gate.
P04e adds independently completed worker reconciliation; see [V1_RECOVERY.md](V1_RECOVERY.md).

## Install the isolated runtime

Proteus retains Python 3.10+ support. Harbor runs in a separate Python 3.12+
environment; importing Proteus does not import Harbor. The bridge checks Harbor
0.24.0's packaged source bytes against commit
`b53b8134e1241686dca7759af188f987ecc48e8b`, then fingerprints its dependency versions,
Python and platform. A recipe binds that fingerprint and rejects a changed runtime.

With Docker/Compose v2 and `uv` available, install the pinned checkout in a new
directory, using its committed lock:

```bash
git clone https://github.com/harbor-framework/harbor.git /tmp/proteus-harbor-source
git -C /tmp/proteus-harbor-source checkout b53b8134e1241686dca7759af188f987ecc48e8b
cd /tmp/proteus-harbor-source
UV_PROJECT_ENVIRONMENT=/tmp/proteus-harbor-runtime uv sync --frozen --no-dev --no-editable --python 3.12
```

Run the following from an installed Proteus checkout, choosing a new study directory:

```bash
python -m proteus.examples.harbor_evaluation \
  --python /tmp/proteus-harbor-runtime/bin/python --out /tmp/harbor-study
proteus recipe lock /tmp/harbor-study/recipe.json --source proteus=. \
  --out /tmp/harbor-study/lock.json
proteus recipe run /tmp/harbor-study/recipe.json --lock /tmp/harbor-study/lock.json \
  --artifact-store /tmp/harbor-study/artifact-store --out /tmp/harbor-study/run
```

The fixture needs no model credentials. It compares A (41), B (42, with an obsolete
file removed) and restored A on a task whose independent verifier expects 42.
`--include-timeout` adds a fourth candidate that exceeds its execution deadline.
The CI job `Pinned Harbor and real Docker trials` runs this acceptance test and
uploads its raw trial records. A skipped local test does not count as this gate.

## Candidate, task and environment binding

- A task input is `{path, tree_hash}`. The adapter captures and verifies the entire
  Harbor task tree, then runs an immutable private copy. Only the public instruction
  enters the candidate's JSON input; verifier source and host task paths do not.
- Candidate programs consume a JSON request on stdin and emit the existing
  `PythonReply` JSON contract on stdout. They can use installed libraries, call model
  APIs and create task artifacts. Images must provide Python 3 and the candidate's
  dependencies; this bridge does not install them implicitly.
- Before executing the candidate, the worker verifies file bytes, paths and executable
  bits inside the container, and inspects the actual Docker image ID and requested
  CPU/memory limits. The witness is retained in controller memory and associated
  with candidate/task hashes and the Harbor trial UUID.
- The separate verifier's container ID, actual image, resource limits and mounts
  are also inspected before grading; it must use a different container from the
  candidate. This evidence stays in the private backend record.
- Image digests, network policy/allowlist and CPU/memory declarations must agree
  with `task.toml`. Phase overrides cannot silently change networking. Unsupported
  custom Compose mounts/services, arbitrary task Dockerfiles, injected skills/MCP tools, legacy
  configuration fields and non-independent state policies fail explicitly.
- Optional `credential_env` lists names of host environment variables passed only
  to candidate execution. Requests and manifests contain their names, not values.
  Candidate output can still disclose its own credentials; it is not a secret scrubber.

## Verifier privacy and failures

Tasks must explicitly set `[verifier].environment_mode = "separate"`; the verifier
uses a fresh copy of the pinned task image. The adapter removes Harbor 0.24.0's
default verifier-log mount from the agent environment. Only agent logs and the
artifact publication directory are mounted there. The pinned single-step trial
subclass makes this policy explicit; a future Harbor upgrade needs new verification.

The trusted adapter returns `BackendEvaluation` separately from the candidate's
output and trace. Its producer, artifact and task binding are checked before
prediction publication. Raw rewards, verifier diagnostics and Harbor config/results
remain under `private/`; only declared feedback is released after prediction or
batch commitment. Hidden evaluations keep their grades private. Python plugins
on the controller remain trusted code; visibility contracts are not a host sandbox.

Agent timeout, invalid candidate output, missing reward and infrastructure failure
remain distinct outcomes. Infrastructure failures are not valid zero scores.
The worker reconciles containers, networks and volumes using the two exact generated
Compose project labels before publishing completion. On bridge interruption the
controller terminates its owned process group and performs fallback cleanup if the
worker could not complete it. It never prunes the
daemon or touches unrelated projects. `cleanup.json` records verified removal or
uncertainty; daemon errors do not count as successful cleanup. No host-execution
fallback is used when Docker is unavailable.

## Accounting

The private SQLite ledger now records subject and grader dispatch, in addition to
managed actor calls and evaluation-cohort reservations. Integrated verifier work
is reserved before the Harbor trial and settled with its private result; mapping
the reward to a `Grade` does not charge it again. Managed subject wrappers delegate
model/cost accounting to their actor entries. Invalid returned contracts preserve
observed usage, and publication failures do not erase executed work.

Arbitrary candidate and verifier programs have unknown model-call/token/cost usage
unless an authoritative backend reports it. Unknown is never converted to zero;
finite model-call caps are rejected for this adapter. Harbor's agent/verifier phase
times are recorded separately; total bridge elapsed time, including setup and
cleanup, is a separate diagnostic in `bridge-usage.json` and is not added twice.
This does not yet provide whole-system native/profiling accounting or generic
remote-operation reconciliation. [Recovery](V1_RECOVERY.md) reuses a settled
trial result across a controller crash without relaunching its agent or verifier.
When the controller result is missing, P04e can instead reconcile the original
worker's sealed completion receipt after its OS lease is released. An active worker
is left running; missing or invalid completion evidence remains uncertain. This
does not issue another trial or grant another budget.
