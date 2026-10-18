# Managed candidate code in Harbor (P07c)

`proteus.harbor_code@1` implements the same prediction and explicit memory-update
protocol as `proteus.local_json`. The subject remains `proteus.code_subject`;
methods do not acquire a second container-specific API. An environment plugin
advertises `environment.code_protocol` and returns an `EnvironmentLease` with a
`code_executor(options, call, actor)` callable. Capability resolution rejects an
opaque environment before a code method can dispatch.

## Execution boundary

Each call gets a fresh Docker container created, uploaded and stopped through
the pinned Harbor 0.24.0 runtime. The fixed provider uses a Docker `none` network
namespace, zero bind/volume mounts, all Linux capabilities dropped,
`no-new-privileges`, a PID limit and explicit CPU/memory limits. Candidate Python
runs as UID/GID 65534 with a cleared environment. Neither model credentials nor
the Docker socket enter the container. The image and Docker host are trusted
infrastructure; this is container isolation, not a VM or a protection against
Docker/kernel vulnerabilities.

Only the supplied model callback crosses to the host actor service. The host
enforces the declared call budget and transport timeout; it records prompts,
responses, runtime and usage in the existing journal/ledger. The child cannot
bypass that accounting with a network SDK call because it has no external
network. The actor itself executes outside the candidate container.

Harbor's buffered command API does not provide bidirectional stdin/stdout, so
the protocol stream uses `docker exec -i` into the already verified Harbor
container. The standalone child preserves an RPC stdout descriptor and sends
candidate stdout/stderr to the diagnostic channel. The same bounded frame,
output, trace and request-count checks apply to both providers.

The host verifies actual container image identity, network mode, resource limits,
mounts, capabilities, privilege policy and PID limit before candidate execution.
It checks the complete uploaded source tree inside the container before import.
These witnesses are stored outside the container in `code-private/loaded.json`
and attached to the normalized trace. Candidate-authored files cannot replace
them. No task reference, grader material or historical evidence is uploaded
implicitly; only the candidate, code call and explicit state are supplied.

## State, builds and deadlines

Independent evaluations reset memory. A sequence carries only explicit
`CodeState`, while its filesystem and processes are recreated at each step.
Persistent worker/container state remains separate future work.

The existing `source_build` option is supported. Its managed build service binds
the input artifact, build specification and resulting image, records cache reuse
and accounts for build work separately. The source witness checks
`/opt/proteus/source`; execution imports the built tree at `/opt/proteus/build`.
Without a build, both use the uploaded candidate. Builds follow their declared
build network policy, independently of the always-offline runtime container.

`execution.timeout_seconds` bounds the full code invocation, including container
startup and model calls. `setup_timeout_seconds` additionally caps setup. Budget
for cold image provisioning accordingly. Cleanup can extend past the execution
deadline, and failure to verify cleanup raises `RecoveryRequired`.

The worker owns only a generated Compose project. Normal completion, callback
failure and timeout stop it and audit removal. A separate process watches a pipe
from the worker, the controller PID and the deadline; it survives their process
group, stops the worker group before cleanup, and records removal after a hard
exit. The worker asks Harbor to stop the environment, but only the parent or
guard seals the final cleanup receipt, after stopping the worker group and any
cancelled setup commands it owns. They serialize the audit through a lock and
bind its receipt to the exact request. No global Docker prune is used.

Settled code calls and predictions use existing no-repeat recovery. A hard exit
before a result is settled remains uncertain even if container cleanup succeeds:
resume stops with `RecoveryRequired` instead of repeating model calls. Resource
cleanup does not fabricate an execution result or release uncertain call usage.

## Example and release gate

Prepare the independently pinned Harbor runtime as in [V1_HARBOR.md](V1_HARBOR.md),
then run the deterministic fixture:

```sh
python -m proteus.examples.harbor_code_memory --out /tmp/harbor-code-study \
  --harbor-python /absolute/path/to/harbor-runtime/bin/python
proteus recipe lock /tmp/harbor-code-study/recipe.json \
  --plugin proteus.examples.code_memory:register \
  --source "proteus=$PWD" --out /tmp/harbor-code-study/lock.json
proteus recipe run /tmp/harbor-code-study/recipe.json \
  --plugin proteus.examples.code_memory:register \
  --lock /tmp/harbor-code-study/lock.json --out /tmp/harbor-code-run
```

This fixture performs five predictions and one learning invocation in six
containers, with seven managed echo calls and zero real model requests.
`tests/test_v1_harbor_code.py` adds actual Docker checks for host-secret and
filesystem exclusion, blocked egress, clean trees, memory transfer, budget
refusal, timeout, source-build execution/cache accounting, hard-exit cleanup and
uncertain resume refusal. Raw requests, source/image witnesses and cleanup
receipts join the existing Harbor CI evidence artifact. Local policy/stdio tests
are not substitutes for this Linux Docker gate.

This batch implements the isolated code provider. Meta-Harness's proposer,
domain data protocol, complete recipe and real-model results remain unfinished.
