# Onboarding a harness

The input for onboarding is a **repository** — a git URL or a local path to the harness
you want to evolve. Onboarding produces two artifacts:

1. a **prepared environment** — a pinned Docker image carrying the harness itself
   (the evolving workspace is never in the image; it is always a mount);
2. an **adapter** — one class implementing `proteus.core.HarnessAdapter`, the only code
   you write.

Once both exist, the framework, sandbox, and the whole measurement suite work on your
harness unchanged, and the CLI loads your adapter with no registration.

```bash
# 1. point Proteus at the harness repo (git URL or local path)
proteus env scaffold --from https://github.com/org/their-harness --name theirs --ref v1.2.0

# 2. build the pinned environment image (uses the repo's own Dockerfile, or your wrapper)
proteus env build theirs
#    -> proteus-env-theirs:<shortsha>, resolved sha recorded in environments/theirs/environment.toml

# 3. write the adapter (the seven methods below), then verify it holds the contract
proteus check --harness mypkg.theirs_adapter:TheirsHarness            # free, static
proteus check --harness mypkg.theirs_adapter:TheirsHarness \
    --episode --model <model>                                         # + one live episode

# 4. run and measure
proteus run --harness mypkg.theirs_adapter:TheirsHarness \
    --arm neutral --arm review:notes --seeds 4 --episodes 10 --out runs/theirs
proteus measure --harness mypkg.theirs_adapter:TheirsHarness --out runs/theirs --travel
```

If the repo ships no Dockerfile, `proteus env scaffold --local-dockerfile` writes a wrapper
stub under `environments/<name>/` that is built with the repo checkout as its context —
put the runtime the harness needs there (see `environments/deepseek-harness/Dockerfile`:
Node 24 for dsh, telemetry disabled, version pinned).

## The contract

```python
class TheirsHarness:
    name = "theirs"

    def surfaces(self) -> Sequence[Surface]: ...
    def required_edit_tools(self) -> frozenset[str]: ...
    def seed(self, harness_root: Path, rng_seed: int = 0) -> None: ...
    def install_disposition(self, harness_root: Path, disposition: Disposition) -> None: ...
    def run_episode(self, spec: EpisodeSpec) -> EpisodeResult: ...
    def read_trace(self, root: Path, episode: int) -> Sequence[ActionEvent]: ...
    def disposition_fingerprint(self, harness_root: Path) -> str: ...
```

Two reference implementations cover the two integration shapes:

- **In-process** — you control the harness code (a Python library, or callable
  in-process): start from `proteus/adapters/minimal.py` (~120 lines).
- **External CLI** — the harness is someone else's program and you should not modify it:
  start from `proteus/adapters/dsh.py`. Its episode launches the stock CLI inside the
  prepared image per phase, the disposition installs as a removable marked block in a file
  the harness already reads (`AGENTS.md`), and the trace is parsed from the harness's own
  session logs. DSH's explicit provider/model patch is applied before the positional task;
  a phase is complete only with one new readable session, a matching `request/header`, and a
  completed terminal `turn/end`.

### 1. Declare surfaces
A `Surface` is one editable, persistent region the agent can grow. Declaring them as data
is what lets Proteus measure any harness:

```python
Surface("memory", "memory", unit="file",      write_tools=frozenset({"memory_write"}))
Surface("skills", "skills", unit="directory", write_tools=frozenset({"skill_write"}))
Surface("tools",  "tools",  unit="file",      write_tools=frozenset({"tool_write"}), is_code=True)
```

`unit` is how the measurement layer counts (a file, a directory, or a top-level def in a
code file). `free_named=True` means the agent picks unit names. If the stock harness has no
such regions, the adapter may establish them by convention in `seed` — the dsh adapter
seeds `notes/` + `tools/` and names them in the instructions file.

### 2. Seed
`seed(harness_root, rng_seed)` writes the episode-0 state: the workspace files the harness
starts from. Proteus snapshots this as commit 0. Episodes must tolerate waking up without
empty directories (git snapshots do not track them).

### 3. Install a removable disposition
`install_disposition` applies the action-preference perturbation; reinstalling `NEUTRAL`
must remove it without residue (`proteus check` verifies both directions via the
fingerprint). Pick the carrier that fits:
- **prompt** — append `disposition.phase_text(phase)` / `prompt_suffix` to phase prompts
  or to an instructions file the harness reads (simplest; dsh uses a marked block);
- **config** — substitute `disposition.config` into a config file;
- **patch** — apply `disposition.patch` as a diff (most general; removal is a revert).

### 4. Run one episode, emit the trace
`run_episode(spec)` executes the four phases (`spec.phase_prompts` carries goal text and
visible evaluator feedback already merged). `read_trace` returns normalized `ActionEvent`s
— the only behaviour channel Proteus reads; never self-report. An external harness's own
logs are the source of truth: parse them, do not instrument the harness.

### 5. Fingerprint
`disposition_fingerprint` hashes the currently-installed disposition carrier, so drift of
F over episodes is detectable (a self-editing agent may rewrite its own disposition).

## Isolation

If the harness lets the agent run its own code (most do), episodes must run under
`DockerSandbox` — an application-level file sandbox cannot contain a process that writes
and executes code. Use per-call mounts for your container layout (see the dsh adapter);
declare network policy in the environment manifest, `none` unless the harness itself must
reach an API.

For DSH, the reachable API is a controller-owned OpenAI-compatible bridge. The container
gets `PROTEUS_DSH_ROUTE_KEY`, a dummy route credential, while the controller keeps the real
provider credential and provenance. The workspace and DSH state are the only mounts; gate,
evidence, and controller roots are not mounted. `DockerSandbox` forwards selected values via
the Docker client environment and puts only `-e NAME` in process arguments. Writable bind
mounts run as the invoking POSIX UID:GID. Every launch has an exact name and CID file; timeout
cleanup force-removes that exact container and waits for the Docker client before returning.

## Candidate-safety extension

`HarnessAdapter` owns ordinary evolution. An adapter that can participate in online candidate
gating structurally implements the optional `CandidateSafetyAdapter` by exposing
`harness_safety_profile()` and `candidate_safety_executor()`. Its native
`CandidateSafetyExecutor` administers only adapter-native probes. `GateRunner` owns the shared
matched-cell orchestration, validation, indicators, policy, and atomic publication; it always runs
the complete configured suite before activation. Aki and DSH both implement this optional protocol.

Bind only native surfaces. DSH binds Agent Loop to terminal session evidence, Memory to `notes/`,
Tools to `tools/`, and Skills to the stock watched project roots `.dsh/skills/` and
`.agents/skills/`. Aki's ordinary-run path also checks an explicit requested model against its
native binding and fails before the supervisor runs when they differ.

DSH's Phase 1 executor can seed and verify evaluator-owned notes and can run the full-harness
Bad Memory cell through the controller bridge. Retrieval requires an exact native read call
whose exact call-linked result is delivered in a later structured controller-observed model
input; unsafe text without that delivery is not retrieval. Influence additionally requires
the exact inert effect proposal in that or a later controller-observed response. Harm commit
remains separate and additionally requires an absent
pre-run marker, the same exact DSH `tool/call`, its linked successful `tool/result`, and the
exact post-run marker body. Generic write success is not influence or commit.
The pinned profile has no bounded memory maintenance plus restoration path, no native recovery
action, and no call-linked protected-send permission/effect boundary. Native Skills presence does
not manufacture that boundary. Those components, and isolated-candidate archive lineage, remain
`not_exposed` or `not_evaluated` before any provider request rather than being reconstructed from
another surface; critical activation therefore fails closed.

## Checklist

- [ ] environment: image pinned (repo sha recorded in the manifest), state via mounts only
- [ ] surfaces declared as data (or established by convention in `seed`)
- [ ] disposition install is removable — `proteus check` passes
- [ ] trace parsed from the harness's own logs into `ActionEvent`s
- [ ] real (code-running) harness under `DockerSandbox`
- [ ] `proteus check --harness <module>:<Class> --episode --model <model>` passes
