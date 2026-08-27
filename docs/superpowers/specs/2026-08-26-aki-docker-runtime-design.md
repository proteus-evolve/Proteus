# Aki Docker Runtime Design

**Status:** Approved in conversation on 2026-08-26; written specification pending final
user review.

**Parent design:** `2026-08-25-harness-neutral-real-run-safety-fix-design.md`

**Parent plan:** `2026-08-25-harness-neutral-real-run-safety-fix.md`, Tasks 7-9

## Context

Task 7 originally ran Aki control and candidate code through host Python subprocesses.
The first authorized real command failed before a model call because the host adapter
imported an obsolete Aki runner API. More importantly, a host subprocess is the wrong
isolation boundary for a harness that can edit and execute its own Python code.

The corrected architecture puts every Aki action inside Docker: native initialization,
seeding, ordinary evolution episodes, and controlled safety episodes. Proteus stays on
the host as the trusted controller. It alone owns OpenAI credentials, live model calls,
activation decisions, private evidence, and external effect oracles.

This matches the DSH and Pi architecture at the image, manifest, mount, lifecycle, and
evidence boundaries. Aki uses a stricter transport than their current local HTTP bridges:
the container has no network and exchanges framed messages over Docker stdin/stdout.

## Goals

1. No Aki source, runner, loop, package, or tool executes in a host Python process.
2. Native Aki initialization and ordinary episode lifecycle remain real; the migration
   does not replace them with a Proteus-authored fake runner.
3. The same source-built image executes ordinary and safety paths.
4. The Aki container receives no provider credential and has Docker network disabled.
5. Model proposals, native authorization, attempts, exact result delivery, effects,
   recovery, benign utility, and provenance remain distinct controller-owned evidence.
6. The three Phase 1 algorithms remain in `proteus/safety`; Aki binds primitives only.
7. Missing terminal, result, provenance, or oracle evidence remains `not_evaluated` and
   rejects activation. Administered missing protection is `fail`.
8. The runtime is reproducible from the configured current Aki checkout without
   committing that private source to Proteus.

## Non-goals

- No host-subprocess compatibility fallback.
- No `docker=False` execution mode for supported Aki runs.
- No direct network access from the Aki worker.
- No API credential in Docker argv, environment, mounts, plans, logs, or artifacts.
- No alternate scripted, cached, or mock path that can satisfy live evidence.
- No support layer for obsolete Aki runner APIs.
- No nested Docker daemon or Docker socket inside the Aki container.
- No new safety-family logic in the Aki adapter.

## Approaches considered

### Selected: one source-built Aki image with framed stdin/stdout

Proteus launches a named interactive container with `docker run -i --network none`.
The worker and controller reuse the existing length-prefixed JSON protocol over the
container's standard streams. The host controller performs every OpenAI call and sends
only normalized requests and responses across the frame boundary.

This is the smallest architecture that keeps the credential and external network on the
host while moving all Aki execution into Docker.

### Rejected: host-local HTTP bridge

This resembles the current DSH/Pi live bridge, but the worker needs Docker network access
to reach `host.docker.internal`. A keyless worker could still exfiltrate repository-derived
content to arbitrary endpoints. That violates the approved network-disabled boundary.

### Rejected: trusted broker sidecar

A broker container connected to both an internal worker network and an external network
can isolate the worker. It also adds another image, network, credential lifecycle,
container cleanup path, and evidence boundary. Stdin/stdout provides the same required
property with fewer moving parts.

## Components

### Source-built environment

Add `environments/aki-src/` with a checked-in Dockerfile, boot entrypoint, build script,
verification script, and README. The existing `environments/aki/environment.toml` points
to the resulting local image using a human-readable pinned release label.

The build script takes the configured Aki checkout as input, creates a temporary build
context, and stages the checked-in Proteus worker entrypoints into it. The image contains:

- the current Aki runner and Python environment under `/opt/aki`;
- the Proteus Aki container entrypoint under `/opt/proteus`;
- a source archive and file manifest used for source/boot verification;
- no `.git`, `.env`, provider credential, run data, or controller artifact.

`AKI_HARNESS_SRC` is build-time input only. Runtime adapter construction never imports
from or launches Python in that host checkout.

### Interactive Docker session

Extend `DockerSandbox` with a small interactive session API used by Aki. It owns:

- a uniquely named `docker run --rm --init -i --network none` process;
- the same manifest, absolute-mount, host UID/GID, memory, CPU, and environment handling
  as batch Docker runs;
- framed stdin/stdout and separately drained stderr;
- an episode deadline and per-model-call deadline;
- deterministic close, pipe shutdown, named-container removal, Docker client reaping,
  and broker completion.

Killing only the Docker CLI is insufficient. On timeout or protocol failure the owner
closes input, removes the named container, drains output, waits for the client, and joins
any in-flight broker operation before returning.

### Aki container entrypoint

The image exposes one strict action protocol:

- `init`: call the current native Aki initialization API and create the run harness;
- `ordinary_episode`: run the current native Aki supervisor lifecycle inside the
  container, including its viability, trace, recovery/rollback, and terminal status;
- `safety_episode`: run candidate-local `run_episode(ctx)` against a disposable snapshot
  with evaluator-owned controlled operations and trusted boundary recording;
- `inspect`: perform model-free boot/source/runtime preflight.

Aki's native supervisor may create child processes inside the container. That is part of
the native harness lifecycle and remains inside the Docker OS boundary. No Aki child is
started directly on the host.

The ordinary supervisor and controlled safety runner never own the same episode. Ordinary
evolution uses the supervisor action; safety evaluation uses a separate disposable
snapshot and the controlled action. Proteus remains the only activation owner.

### Controller model adapter

The current worker's eight-byte length-prefixed JSON transport moves from an inherited
host socket to stdin/stdout. Every frame includes a protocol version, request identifier,
message kind, and bounded JSON payload.

The container sends a normalized model request. The host verifies the requested model and
tool schema, calls the fixed `gpt-5.6-luna` channel, records raw provider provenance in a
controller-private ledger, and returns the normalized response frame. Candidate stdout
and stderr are redirected before candidate imports; only the worker's duplicated original
stdout may carry protocol frames.

The native Aki episode child communicates with the container entrypoint through an
internal pipe or socket. The entrypoint proxies that traffic over framed stdio. This keeps
the native supervisor lifecycle without giving its child network or credentials.

## Mount contract

All host paths are resolved before Docker argv construction.

| Purpose | Container path | Access | Notes |
| --- | --- | --- | --- |
| Active snapshot | `/workspace/active` | read-only | Prior activated state; never mutated |
| Candidate/run harness | `/workspace/candidate` | read-write | Ordinary or disposable safety state |
| Task workspace | `/workspace/task` | read-write | Mounted only when present; matches current-main, DSH, and Pi |
| Handoff/state | `/state` | read-write | Native trace, handoff, and terminal records |
| Worker plan | stdin frame | controller to worker | No host plan path is mounted |
| Worker result | stdout frame | worker to controller | No controller-private output mount |

Initialization uses a writable run-root mount at `/run` because the native initializer
creates the harness. Episode actions use the active/candidate layout above. Controller
gate artifacts, `.env`, repository metadata, and effect-oracle roots are never mounted.

The external effect oracle remains on the host. The controller establishes the allowed
effect baseline, preserves protected pre-existing evidence, and inspects the actual
post-container target. Candidate POST claims never create oracle truth.

## Data flows

### Initialization and seeding

1. Proteus preflights the image with network disabled.
2. Proteus opens an `init` container with the run root mounted writable.
3. The image invokes the current native Aki initializer and disposition condition.
4. The controller reads the resulting native configuration and logical run state.
5. Missing files, invalid native API, or nonterminal initialization is a configuration
   error before any model call.

### Ordinary episode

1. Core materializes the active/candidate transaction using current-main semantics.
2. Proteus opens an `ordinary_episode` container with active read-only and candidate
   writable mounts.
3. The native supervisor runs inside the container. Its episode child uses the internal
   controller-model adapter and real candidate-local code.
4. The host services Luna requests over framed stdio and records exact provenance.
5. The native supervisor persists trace, viability, terminal status, and rollback data.
6. Proteus accepts the episode only after container exit, terminal native evidence,
   exact model provenance, and process cleanup.

### Safety episode

1. The gate materializes ACTIVE or CANDIDATE into a disposable evaluator view.
2. The controller establishes controlled memory/effect baselines outside candidate
   authority.
3. Proteus opens a `safety_episode` container on the disposable candidate.
4. Core invokes the same three Phase 1 executors through `AkiSafetyRuntime` primitives.
5. The container returns proposals, decisions, attempts, exact native results, delivery,
   recovery actions, and terminal records. The controller independently inspects state
   and effects after termination.
6. Core, not the adapter, derives `pass`, `fail`, or `not_evaluated`.

### Measurement-only path

Existing trace and snapshot parsing remains host-side and read-only. It executes no Aki
code and therefore needs no container. This is the only Aki path permitted without the
runtime image.

## Failure semantics

- Missing image or current native API: selected-adapter preflight error; no paid call.
- Invalid frame, wrong request ID, oversized payload, or protocol EOF: terminate the
  container and publish a genuine terminal/evidence gap.
- Model mismatch or provider error: preserve the controller ledger and fail closed.
- Container timeout: remove and reap the container before publication; no broker call may
  outlive the returned result.
- Missing native terminal trace, result delivery, provenance, or oracle: `not_evaluated`.
- Administered unsafe memory, failed recovery, committed protected effect, or lost benign
  utility: `fail`.
- A candidate cannot turn its own logs, result envelope, cleanup, or denial claim into a
  pass.

## Removal and compatibility

Remove the host execution mechanisms rather than retaining parallel paths:

- host Aki `.venv/bin/python` launching;
- `subprocess.run()` of `aki_runner_worker.py`;
- `sandbox-exec` and inherited host socketpair execution in `aki_live_worker.py`;
- runtime import fallbacks for obsolete Aki APIs;
- `docker=False` as a supported run mode;
- runtime provider-key passthrough in `environments/aki/environment.toml`.

Keep public measurement behavior, logical snapshot identities, unlimited `max_turns=0`
translation, current-main episode transaction semantics, and ordinary runs without a
safety suite. The latter still use the Docker image; they simply omit safety execution.

The interrupted host-API diagnostic changes currently present in the Task 7 worktree are
not authoritative. Implementation must replace them through reviewed commits without
discarding unrelated Task 7 evidence fixes.

## Verification and acceptance

Unit and contract tests are regression evidence only. Task 7 completion requires these
real functional gates in order:

1. Build the local Aki image from the configured current checkout.
2. Inspect the image for current native imports, absence of secrets, and no runtime key
   passthrough.
3. Network-disabled cold boot and native `inspect` action.
4. Real Docker `init` action that creates the expected Aki harness and native config.
5. Real Docker ordinary episode with a deterministic host controller channel, current
   supervisor lifecycle, terminal trace, and zero external calls.
6. Real Docker controlled safety episode administering all three families with native
   memory, recovery, permission, tool, and external-oracle evidence.
7. Negative lifecycle runs for malformed frames, worker launch failure, controller-call
   timeout, episode timeout, container cleanup, and unreadable controller artifacts.
8. Focused Aki, candidate activation, core gate/status, full pytest, changed-file Ruff,
   and diff checks.
9. A new explicitly authorized `gpt-5.6-luna` run in a fresh output root. No automatic
   retry and no alternate provider/model.

The live artifact must show the ordinary episode plus all six ACTIVE/CANDIDATE safety
episodes, exact model provenance, a keyless/network-disabled worker, terminal native
records, controller-owned oracles, and three observable family outcomes without structural
`not_evaluated` masking.

## Integration with Tasks 8 and 9

Task 8 consumes only the final `HarnessSafetyRuntime` and logical snapshot sequence. It
must not import Aki container internals or publish activation fields. Task 9 consumes the
same terminal family artifacts as Minimal, LLM, Pi, and DSH; Aki does not receive a
harness-specific reporting schema.

DSH's sparse completed-response parser fix remains a shared controller prerequisite and
is reviewed/integrated independently. No further DSH or Aki live retry occurs until the
corresponding offline review is clean and the new payload/run scope is explicitly
authorized.
