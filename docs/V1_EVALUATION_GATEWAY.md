# Worker-submitted formal evaluations

P09c adds an EvaluationGateway between persistent research workers and Proteus's
existing candidate, evaluation, evidence and shared-knowledge services. A worker
submits candidate bytes and a declared evaluation ID. The controller evaluates
those immutable bytes and returns the evidence authorized by its recipe.

This is infrastructure for the CORAL recipe. CORAL's reference implementation
at `0123dfb939b35228cf2c1fde224cd0561e727408` submits autonomous evaluations through
its post-commit hook, evaluation CLI and grader daemon. Its separately named
`coral/gateway` is a model proxy; this component does not claim to reproduce that
proxy. CORAL intervention policies, mediated knowledge editing, provider-specific
worker bridges and real model experiments remain subsequent work.

## Configuration and control flow

`RecipeConfig.gateways` contains `GatewaySpec` declarations:

| Field | Meaning |
| --- | --- |
| `id` | Gateway identity within the locked recipe |
| `members` | Worker ID to evidence-owning stage ID |
| `evaluations` | Allowed, observed evaluation IDs; hidden tests are rejected |
| `knowledge_space` | Optional shared space for formal attempt records |
| `max_pending_per_worker` | Maximum accepted requests that remain queued or processing |
| `max_evaluations_per_worker` | Cumulative task admission limit for each member |
| `max_request_bytes`, `max_files` | Upload limits; defaults are 8 MiB and 1024 files |

Each member also sets `WorkerSpec.gateway` to this gateway. Its evidence stage
must belong to the worker's role and cover the gateway's evaluations. A configured
knowledge space must permit that role and the stage's full evidence grant. The
stage declares access; returning a view does not invoke its operator.

Methods serve the transport and explicitly drive the formal evaluation pump:

```python
gateway = services.gateway("formal")
with gateway.serve():
    worker = services.worker("researcher")
    launch = worker.start("search", "Explore and submit candidates for evaluation.")
    # Use stable operation IDs and preserve this control flow during replay.
    result = gateway.step("next-proposal", timeout_seconds=30)
    # Continue the method's declared loop and observe worker completion.
```

`step` is a journaled FIFO `claim` followed by `process`. Claim and processing can
also be separate calls. Processing runs on the method thread; HTTP handlers only
admit frozen candidates and read durable responses. The queue does not dispatch
evaluations automatically. A method must drain or explicitly handle its accepted
requests and finish its workers before publishing a terminal result.

The first implementation has one serial evaluation pump per run. Existing
evaluation configuration can parallelize an independent task cohort. Concurrent
research workers do not imply concurrent gateway requests are evaluated at once.

## Worker interface

The command worker runtime receives `PROTEUS_EVALUATION_URL` and
`PROTEUS_EVALUATION_TOKEN` in its environment. Other runtimes must declare
`worker.connection_environment` and implement that connection injection contract.
The bearer token determines worker identity; request bodies cannot choose another
worker or supply a score, private result reference or task subset.

```sh
python -m proteus.gateway.client submit ./candidate \
  --operation proposal-001 --evaluation validation
python -m proteus.gateway.client wait TICKET_ID --timeout 300
```

The client captures regular files and executable bits, rejects symbolic links and
special files, and checks two snapshots for concurrent edits. This detects changes
but is not an atomic filesystem snapshot. The submitted bytes become immutable;
later workspace edits cannot change the candidate being evaluated. The receiver
decodes through the subject's artifact codec and rejects uploads whose canonical
representation would differ from the supplied files.

`POST /v1/submissions` returns a ticket. Repeating an identical request with the
same worker and operation ID returns the same ticket, including after completion.
Changing any request content under that ID is rejected. A client that loses the
POST response should retry the identical submission. `GET /v1/submissions/TICKET`
returns `queued`, `processing`, `completed` or `failed`; another worker cannot poll
that ticket. `GatewayClient.wait` tolerates connection failures during a controller
restart. HTTP errors remain explicit to the caller.

A completed response includes its exact candidate, an immutable `EvidenceView`
and base64-encoded files from that view. Disclosure is restricted to the ticket's
candidate and the member stage's role, evaluations, splits and evidence kinds.
Optional shared attempts are published by the controller from the same released
evidence. A failure returns a generic diagnostic without a grader's exception
payload; unavailable results are not fabricated as worker-supplied scores.

## Budgets and recovery

Admission is transactional in SQLite. Both pending capacity and cumulative task
admission limits are rechecked when the request is committed. The admission count
uses the entire configured task cohort. Accepted requests consume that allowance
even if evaluation fails; repeated identical submissions consume no extra
allowance. This conservative queue allowance is separate from actual usage in
the run ledger, which reserves and settles formal evaluations through the existing
evaluation service. Worker generation budgets cover the worker runtime itself;
gateway task admission is controlled by `max_evaluations_per_worker`.

Formal evaluation records include the submitting worker ID. Worker and evaluation
execution share the global concurrency budget: reserve headroom for formal
evaluation while persistent workers are active. Unknown provider usage remains
unknown. This gateway does not make the command backend capable of enforcing
provider cost or model-call bounds that the backend cannot substantiate.

Queue admissions, FIFO claims (including empty claims), and responses are durable
independently of method journal commits. On replay an earlier claim keeps its
original ticket even if new requests arrived later. The method replays the same
candidate/evaluation/evidence operations; it must not skip them merely because a
queue row already says completed. Committed evaluation results and shared attempts
are reused rather than evaluated or published again. Uncertain external work
continues to use the evaluation backend's reconciliation protocol.

The gateway reopens the recorded loopback address and keeps its original member
credentials after a controller exit. One host lease prevents concurrent servers.
An occupied port, a missing endpoint after worker activity, or a missing previously
initialized queue fails rather than silently changing the address, resetting
credentials or granting a new admission budget. A response can be read from its
durable evidence even while the method journal is replaying an older snapshot.

## Scope of isolation

The first transport is authenticated HTTP on `127.0.0.1`, for trusted local worker
bridges. Credentials are stored in the private queue and passed through runtime
environment injection; they are not recipe fields or public response data. This
is not a security sandbox against another process with access to the same host
account, filesystem or process environment. Remote/container worker transport,
network exposure and TLS are not enabled by this local transport.

Submitted candidates may use the existing Harbor/Docker subject backend. In that
case candidate execution and the separate verifier retain Harbor's established
isolation checks. The research worker itself remains a local command process.

## Executable example and acceptance

```sh
python -m proteus.examples.evaluation_gateway --out gateway-study
proteus recipe lock gateway-study/recipe.json \
  --plugin proteus.examples.evaluation_gateway:register \
  --source "proteus=$PWD" --out gateway-study/lock.json
proteus recipe run gateway-study/recipe.json \
  --plugin proteus.examples.evaluation_gateway:register \
  --lock gateway-study/lock.json --artifact-store gateway-study/artifact-store \
  --out gateway-study/run
proteus recipe resume gateway-study/recipe.json \
  --plugin proteus.examples.evaluation_gateway:register \
  --lock gateway-study/lock.json --out gateway-study/run
```

The offline example starts two actual subprocess workers, each uploads code and
then changes its workspace. It performs one private test and two full two-task
validation cohorts, returns only released validation feedback, and publishes two
formal shared attempts. Its fixed proposals are fixtures, not learned improvements.

Tests inject hard exits after admission, claim, evaluation completion and response
publication, before and after journal commits. They verify original worker
generations, five predictions, unchanged evaluation charges and two shared
attempts. Additional checks cover concurrent admissions/claims, queue limits,
cross-worker access, private feedback, durable state loss and terminal-state
validation. The installed wheel/sdist check runs the public CLI outside the
checkout. The Harbor CI acceptance exercises two autonomous submissions, frozen
code, distinct candidate/verifier containers, original rewards, cleanup and resume.
Its saved evidence is under `evaluation-gateway` in `harbor-trial-evidence`.

This changes the experimental recipe schema. Previously locked runs require their
original implementation; no automatic migration or 1.0 release is asserted.
