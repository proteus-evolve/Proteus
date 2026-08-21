# DSH audio evolution campaign

This is the reproducible setup behind the public experiment **Can DSH learn to hear?**
The driver is the released Proteus **v0.1.0** engine; the versioned campaign entry point,
benchmark, and privacy-reduced publisher live in this repository. The baseline is DeepSeek
Harness `dsh-v0.1.0-rc.8`, exact commit
`141eb6fef83422698aef7a981029e843e8161534`. rc.8 added native image requests and image
inputs for commands, but its ACP adapter explicitly rejects audio, its attachment UI is
image-only, and its DeepSeek wire vocabulary has no audio part.

The experiment targets **harness-level audio**, not an undocumented model feature. The
evolved harness should accept and retain audio, transcribe it through a configurable seam,
and ground the agent with the resulting transcript. It must not invent a native DeepSeek
audio request field: the public DeepSeek Chat Completions API currently documents text
content and DeepSeek Harness's rc.8 extension adds images, not audio.

## Build the exact baseline

```bash
python3 -m pip install 'proteus-evolve[dsh]'

DSH_BUILD_ROOT="$(mktemp -d)"
DSH_CONTEXT="$DSH_BUILD_ROOT/deepseek-harness"
git clone --depth 1 https://github.com/deepseek-ai/deepseek-harness "$DSH_CONTEXT"
git -C "$DSH_CONTEXT" fetch --depth 1 origin tag dsh-v0.1.0-rc.8
git -C "$DSH_CONTEXT" checkout dsh-v0.1.0-rc.8
test "$(git -C "$DSH_CONTEXT" rev-parse HEAD)" = \
  141eb6fef83422698aef7a981029e843e8161534
cp environments/dsh-src/boot.sh "$DSH_CONTEXT/.proteus-boot.sh"
docker build --network host -f environments/dsh-src/Dockerfile \
  -t proteus-env-dsh-src:0.1.0-rc.8 "$DSH_CONTEXT"

proteus check --harness dsh
```

## Run the campaign

The versioned Python entry point fixes the public condition at one neutral arm, one seed,
12 context-fresh episodes, a 100-call budget, and benchmark feedback visible on the next
episode:

```bash
export DEEPSEEK_API_KEY=...
python examples/dsh_audio_evolution.py --out runs/dsh-audio-live
```

Resume the same run after an interruption:

```bash
python examples/dsh_audio_evolution.py --out runs/dsh-audio-live --resume
```

To run the experiment and publish every completed episode to the public Evolving Lab with
one command, authenticate GitHub once with `gh auth login` (or export a fine-grained
`GH_TOKEN` with Contents write permission on the deployment repository), then add
`--live`:

```bash
export DEEPSEEK_API_KEY=...
python examples/dsh_audio_evolution.py \
  --out runs/dsh-audio-live \
  --live
```

The launcher maintains a heartbeat while an episode is in progress. `Ctrl-C` publishes a
`paused` state; the same command with `--resume --live` continues from the last completed
snapshot rather than paying for an episode twice. The public page shows a red live entry,
polls every 15 seconds, and links every visible point to its episode details.

The equivalent generic CLI accepts the same built-in capability benchmark:

```bash
proteus run --harness dsh \
  --goal "$(python -c 'from proteus.bench.dsh_audio import GOAL_TEXT; print(GOAL_TEXT)')" \
  --evaluator dsh-audio@observe \
  --arm neutral --seeds 1 --episodes 12 \
  --max-turns 100 --min-turns-per-phase 5 --announce-budget \
  --out runs/dsh-audio-live
```

## What is measured each episode

`dsh-audio-capability` is a dense source-capability benchmark kept outside the evolving
harness. Its six equally weighted gates are:

1. an audio content block in the provider-neutral LLM vocabulary;
2. durable audio admission and storage;
3. affirmative ACP audio admission rather than rc.8's explicit rejection;
4. Web composer intake and a history/player presentation;
5. a configurable, provider-neutral transcription seam;
6. affirmative audio tests across at least four integration layers.

The rc.8 baseline scores **0/6**. This rubric supplies progress feedback; it is not the
release verdict. Before calling an evolved snapshot successful, run the upstream build and
held-out end-to-end fixtures for WAV, MP3, M4A, OGG, and FLAC, including malformed input,
byte/duration limits, transcription failure, `/goal` and `/plan`, normal conversation, and
the existing image suite. The public claim should report those results separately from the
six-gate evolution score.

## Publish the episode feed

The public website is static. A sidecar process converts finished Proteus episodes into a
small privacy-reduced JSON feed and optionally updates the deployment repository. It
exports scores, structural diffs, normalized tool names, and touched paths. It does **not**
export assistant prose, tool arguments, source contents, prompts, or credentials.

For a local preview:

```bash
python scripts/dsh_audio_live.py \
  --sweep runs/dsh-audio-live \
  --out web/static/assets/dsh-audio-live.json \
  --watch 15
```

For the public site, the `--live` launcher above is preferred. The publisher can also run
on its own; it uses `GH_TOKEN` when present and otherwise uses an existing GitHub CLI login:

```bash
export GH_TOKEN=...
python scripts/dsh_audio_live.py \
  --sweep runs/dsh-audio-live \
  --out /tmp/dsh-audio-live.json \
  --watch 15 \
  --repo proteus-evolve/proteus-evolve.github.io \
  --remote-path assets/dsh-audio-live.json
```

Each changed feed creates an auditable commit. The website polls the feed every 15 seconds
and adds a clickable point only after an episode is complete, so viewers never see an
episode that has not been snapshotted and measured. Heartbeats are used only to distinguish
`running` from `paused`; they are not published as a new commit every 15 seconds.

## Select episodes for posts

Do not pick episodes merely because their score rose. Shortlist an episode when at least
one of these is true:

- it discovers a missing architectural layer or reverses an earlier approach;
- it creates a reusable abstraction rather than adding a one-off patch;
- it breaks the build, diagnoses the failure, and recovers in a later episode;
- the benchmark stays flat while the trace reveals necessary groundwork;
- it connects previously separate image, attachment, command, or ACP pathways;
- it produces a surprising but defensible design choice.

Write one sentence that says what the episode actually did, then place the raw diff,
normalized trace, score change, and manual interpretation beside each other. Editorial
summaries may be supplied as a JSON object such as `{"4": "...", "7": "..."}` through
`scripts/dsh_audio_live.py --editorial summaries.json`; the site labels them as editorial.
