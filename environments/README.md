# Prepared environments

`environments/` contains two kinds of pinned container environment:

1. **Manifest-backed environments** — an `environment.toml` names a local Dockerfile tag or
   a published `docker_image`. `proteus env build <name>` resolves and builds this shape.
2. **Source-mode environments** — `dsh-src/` and `pi-src/` are built with a pinned upstream
   checkout as their Docker context. The image bakes the exact source, dependencies, build
   toolchain, and boot wrapper that let the run evolve and rebuild its own real source.

The manifest short-circuit follows
[Harbor](https://github.com/laude-institute/harbor)'s task config. Source-mode build commands
live in each directory's README because their context is the upstream checkout, not this
repository.

An environment answers one question: *what does this harness need to run an episode that
the host should not have to provide?* That includes runtimes, system packages, the pinned
harness baseline, and—in source mode—the rebuild toolchain. Evolving state never lives in
the image; the adapter extracts source and writes state into mounts, so one image remains
reusable across runs and arms.

## Manifest

```toml
[environment]
name = "their-harness"
image = "proteus-env-their-harness:1.2.0" # local tag the Dockerfile builds
# docker_image = "..."                  # set to a registry ref to skip the build
network = "host"                        # none | host (episodes that need an LLM API)
memory = "2g"
cpus = 2.0
env_passthrough = ["OPENAI_API_KEY"]    # host env the episode may read

[harness]
adapter = "mypkg.adapter:TheirHarness"  # adapter that drives this environment
workspace_mount = "/workspace"          # where the evolving harness is mounted
state_mount = "/state"                  # harness-internal state (sessions, caches)
```

`proteus.sandbox.SandboxConfig.from_manifest(path)` loads the `[environment]` table.

## Status

| environment | harness | adapter | status |
|---|---|---|---|
| `dsh-src/` | [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) rc.7 | `dsh` | **default**; exact source evolution, live release-smoke verified |
| `pi-src/` | [Pi](https://github.com/badlogic/pi-mono) v0.84.2 | `pi` | **default**; exact source evolution, live release-smoke verified |
| `deepseek-harness/` | DeepSeek Harness rc.7 | — | legacy workspace-only image; not used by the current `dsh` default |
| `pi/` | Pi v0.84.2 | — | legacy installed-package image; not used by the current `pi` default |
| `aki/` | Aki research harness | `aki` | image assembled from the research checkout (private); adapter live-verified |
| `openhands/` | [OpenHands](https://github.com/All-Hands-AI/OpenHands) | — | manifest only; adapter not written |
| `swe-agent/` | [SWE-agent](https://github.com/SWE-agent/SWE-agent) | — | manifest only; adapter not written |

Rules for adding one (borrowed where noted from Harbor's conventions):
- **Pin everything**: base image tag + harness version in the Dockerfile; never `latest`.
- **Name images `proteus-env-<name>:<harness-version>`** so cleanup can match the prefix.
- **State out of the image**: the evolving harness, session/build state, and optional task
  workspace are mounts. Proteus snapshots only the measured `harness/`; session state and
  the benchmark exercise remain sibling run artifacts.
- **Source mode must boot the exact run tree**: deletions and renames remove pristine
  files, build outputs are cleared before rebuild, cache keys include paths and contents,
  and an untouched source takes the pristine fast path.
- **Run as the host uid/gid** when a container writes bind mounts, or Linux runs leave
  root-owned state the host cannot restore or snapshot-clean.
- **Network is a declared property** of the environment, not a flag someone remembers:
  `none` unless the harness itself must reach an API.
- Disable any harness telemetry in the image (e.g. `DSH_TELEMETRY_MODE=DISABLED`).
