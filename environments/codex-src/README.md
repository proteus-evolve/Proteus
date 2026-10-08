# Codex source-evolving environment

This environment makes the **real `openai/codex` Rust source** a Proteus `loop` surface.
The Docker image pins an upstream commit, warms Cargo dependencies/build output, and stores
an exact source tar at `/opt/codex-source.tar`. Each Proteus run extracts that tar into
`harness/src/`.

During an episode, the frozen active harness is mounted read-only at `/workspace` and the
candidate is mounted at `/workspace/candidate`. Model-driven phases run as the host user and
only execute the binary pair for the active source hash. The controller mounts that exact
`.codex-builds/<source-hash>/` directory read-only at `/opt/proteus-bin`; authentication,
sessions, PATH aliases, and app-server state remain in the separate writable `.codex-state`.
A candidate therefore cannot replace the executable used by a later phase.

The candidate-boundary gate runs as **container root** in a clean `network=none` container.
It receives only the candidate source as a read-only mount and a fresh output directory—no
authentication, model environment, user mounts, or extra Docker arguments. It overlays the
changed source onto the image's baked `/opt/src` and compiles with the image's own Cargo home
and target dir (`/usr/local/cargo`, `/opt/codex-target`) — the exact paths the image cache was
built with —
so Cargo's fingerprints stay valid and only files whose contents really changed recompile
(overlay uses `rsync --checksum`; snapshot mtimes are never trusted). Two stages run over
the offline cache: `cargo test --lib --no-run` for `codex-tui`/`codex-core`/`codex-cli` (a
release build skips `#[cfg(test)]` code, so this catches candidates whose test modules no
longer compile), then the release build of `codex-cli` + `codex-code-mode-host`. After the
version probe succeeds, the controller checks the output and atomically caches it by source
hash. Validation does not activate it: the next episode selects binaries from its frozen
active snapshot, so an evaluator-rejected candidate cannot disturb the accepted runtime.
The controller records the selected source hash and publication path in
`traces/epNNN-runtime.json`. The release smoke compares this evidence to the preceding
episode's committed source, so a later successful candidate build cannot stand in for
proof that the edited binary pair actually ran in the next episode.
The adapter allows up to 120 minutes for this boundary (`BOOT_TIMEOUT_S`); a high-fanout
core edit may need to ThinLTO-link the large release binaries even with warm dependencies.
The timeout only widens the wait, never the build-success condition. The image also records
its `CARGO_BUILD_JOBS` build argument and reuses that fixed, non-secret value at the boundary.

Build:

```bash
docker build --platform linux/amd64 \
  -t proteus-env-codex-src:test-compile environments/codex-src
```

This image is intentionally `linux/amd64`-only because the pinned OpenAI rusty-v8 artifact
is `x86_64-unknown-linux-gnu`. Arm hosts need Docker's amd64 emulation for this build/run.

Authentication options:

1. `OPENAI_API_KEY` or `CODEX_API_KEY`; or
2. log in with Codex on the host (`~/.codex/auth.json`). `CodexHarness` copies only that
   auth file into the run-private `.codex-state/codex-home/`, outside the evolution history.

Smoke:

```bash
proteus check --harness codex
proteus check --harness codex --episode
```

Example sweep:

```bash
proteus run --harness codex \
  --arm neutral --arm review:skills --arm review:loop \
  --seeds 2 --episodes 3 --max-turns 80 \
  --out runs/codex-smoke

proteus measure --harness codex --travel --out runs/codex-smoke
```

`codex exec` does not expose a native max-tool-call flag. Proteus polls the JSONL log to
stop a phase at its budget and checks the remaining budget before starting another phase;
calls completed between polls can overshoot the limit.

Each completed native tool item counts once. A patch touching multiple files remains one
`file_change` event; `params.changes` preserves each path, change kind, and surface. The
event's `surface` is set when all paths target the same surface, otherwise it is `None`.
Diagnostics are non-tool events and do not consume the budget. Re-reading archived native
logs applies this normalization; previously saved progress counters are not rewritten.

On a failed phase, Proteus prefers the JSONL `turn.failed` diagnostic, then a top-level
`error`, then stderr. The bounded, redacted reason is saved with the failed candidate and
carried through the controller notice and phase prompts on resume. An error followed by
a successful native execution does not by itself fail the episode.
