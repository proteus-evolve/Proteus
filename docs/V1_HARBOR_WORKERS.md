# Harbor persistent research workers

`proteus.worker.harbor@1` uses the same worker identity, immutable generations,
session protocol, budget ledger and method journal as the command runtime. Harbor
creates a new pinned Docker container for each generation. Only that worker's
workspace is mounted, so its files and native CLI session state survive while
containers are removed between generations.

This adapter is under development. Its live Docker gate must pass before merge;
the deterministic fixtures do not establish real-model CORAL performance.

## Configuration and execution

Declare `HarborWorkerOptions` through the worker's runtime plugin. Required inputs
are the absolute Python path for the isolated pinned Harbor installation, its
runtime fingerprint, immutable image digest, absolute container command, content
pins for its executable and wrapper files, and a numeric non-root `uid:gid`.
The chosen uid must be able to write the host-owned workspace. CPU, memory, PID,
setup, generation and interruption limits are explicit options.

The command implements the common worker JSON protocol. Its stdin carries the
launch and confirmed preceding session totals; stdout carries progress and
session/result records. Named provider credentials and scoped gateway connection
values pass through stdin to the trusted container bootstrap, then to the child
environment. Credential values are not Docker command arguments, Compose
environment fields or serialized launch options. Raw agent output is retained,
so this does not claim automatic log redaction.

The packaged stdlib `codex_bridge.py` is also uploaded at
`/opt/proteus/codex_bridge.py`. A prepared image containing the pinned Codex CLI
can invoke it with an explicit bridge specification and corresponding container
file hashes. The host's Python or Codex executable paths are not automatically
valid inside that image. The [CORAL original-task recipe](V1_CORAL_CIRCLE_PACKING.md) and
[full controller/container checks](V1_CORAL_CONTAINER_STUDY.md) compose these
interfaces. Real provider experiments and final release acceptance remain required.

## Filesystem and network boundary

The generated Compose configuration grants one read/write bind mount:
`/workspace`, backed by this worker's own directory. The controller's ledger,
other workers, private grader files and Docker socket are not mounted. Processes
drop all Linux capabilities and cannot gain new privileges; the research command
runs as the declared non-root user. Runtime inspection verifies the exact image,
resource limits, mount source/destination, network and process restrictions.

Two network modes are supported: `none` and explicit `public`. There is no claim
of destination filtering in public mode. Gateway access currently requires
public networking and `GatewaySpec.bind_host="0.0.0.0"`; the default gateway
continues to bind loopback. The container receives the same authenticated worker
credential at `host.docker.internal:<port>`, with a Docker host-gateway mapping.
Local command workers keep the loopback URL. The gateway preserves its address
and configured bind mode during recovery and retains the same evidence grants.
Remote/multi-host transport and TLS are outside this local Docker adapter.

## Stop, recovery and evidence

The host supervisor retains its original immutable launch identity. A separate
`execution.json` binds the derived trusted supervisor command to that identity;
the backend never replaces the request used for ledger accounting.

A host bridge creates and inspects the Harbor environment, uploads its trusted
bootstrap, and streams the provider's worker protocol through `docker exec`.
Pause targets the container child's own process group. The independent guard
outlives a killed host bridge, ends its remaining processes, removes only the
generated Compose project's resources, and records the final cleanup audit.
The controller exposes a terminal completion and releases its reservation only
after cleanup is verified. An absent or failed cleanup audit keeps the generation
uncertain, preventing workspace reuse and duplicate dispatch. This does not
provide automatic recovery from an unavailable Docker daemon or a lost guard.

Private generation evidence includes `harbor-request.json`, dispatch, inspected
container/mount proof, stdout/stderr hashes, and `harbor-cleanup.json`. Live CI
must verify pause/native continuation across different containers, workspace
persistence, inaccessible private host files, and two isolated workers exchanging
knowledge and submitting frozen candidates through the formal gateway.

This extends launch execution records and recipe configuration. Existing locked
runs require their original implementation; no implicit run migration is supplied.
