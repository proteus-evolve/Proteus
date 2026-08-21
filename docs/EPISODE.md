# The episode: what updates when, and who owns it

Code: [`proteus/core/episode.py`](../proteus/core/episode.py) (the loop),
[`proteus/core/continuity.py`](../proteus/core/continuity.py) (phase handoffs),
[`proteus/core/goal.py`](../proteus/core/goal.py) (goals and evaluators),
[`proteus/core/snapshot.py`](../proteus/core/snapshot.py) (snapshots),
[`proteus/core/adapter.py`](../proteus/core/adapter.py) (the adapter contract).

The whole division of labour is one sentence: **the framework owns everything that is
not the harness; the adapter owns everything that is.** How the four phases execute
*inside* an episode is the harness's business. What happens *between* episodes is
identical for every harness — which is what makes a no-goal Aki run and a
goal-conditioned pi run readable with the same ruler.

---

## At run start (once per seed, not per episode)

| step | owner | what happens |
|---|---|---|
| 1. `adapter.seed(harness_root, rng_seed)` | adapter | lay down the episode-0 state: surface directories, initial instructions; dsh/pi also extract their own code from the image into `harness/src/` |
| 2. `adapter.install_disposition(...)` | adapter | install the action-preference perturbation — must be removable; the carrier is the adapter's choice (§ contract) |
| 3. task seeding (benchmark runs only) | framework | `seed_task` writes the benchmark task into `<run>/task/`, beside the harness and outside its snapshot; dsh/pi mount it at `/workspace/task` |
| 4. `snapshot.init(harness)` | framework | bare git repo; **every ignore rule disabled** (the harness is the measured object — nothing in it may be invisible to the instrument); commit `episode 0` |

Resume (`run(cfg, start=N)`) skips all four and continues on the evolved harness on
disk from episode N+1. `completed_episodes` counts **contiguous snapshot commits**, not
trace files — a provider outage writes a trace per failed attempt.

## Every episode N

```
assemble prompts → [boot gate] → run the episode → read the trace → run all evaluators
→ selection (accept/reject) → snapshot → records & feedback → episode N+1, context-fresh
```

### 1. Assemble the four phase prompts — framework

`_phase_prompts` starts from the four base texts (observe / propose / act / reflect) and
applies fixed rules:

- the **goal text** (freeform, decoupled from evaluators) joins **all four phases** because
  a fresh observe or propose otherwise investigates and plans against the wrong objective;
- **last episode's OBSERVE-visible evaluator feedback** joins the **observe** phase
  (HIDDEN results never appear here);
- the **disposition's phase text** joins each phase — *unless* the adapter declares
  `disposition_in_files = True` (dsh/pi carry the perturbation in `AGENTS.md`; adding
  the prompt copy would double the dose, through a channel outside `F`).
- for `continuity_mode="framework"`, the portable handoff protocol joins every phase;
  native and deliberately independent harnesses receive no file-specific instructions.

The agent sees these texts and nothing else. It is never told why.

### 2. Boot gate — adapter (harnesses with a self-editable code surface)

When `harness/src/` exists, `check_boot()` runs `--version` through the exact boot path
the episode will use. For pi that includes the rebuild, so a type error the agent wrote
into its own source surfaces here — exit 97 with the build log tail — **before any API
spend**. A copy that cannot boot fails the episode legibly; the snapshot chain still
holds every prior state.

### 3. Run the episode — the adapter's core

`adapter.run_episode(spec)`, where spec carries the run root, episode number, model, the
four phase prompts, `max_turns`, and the seed. *How* the phases execute is entirely the
adapter's:

- **minimal / llm** (in-process): the four phases run in the framework process, one
  JSONL trace line per step; `max_turns` is a **hard cap** — stop cleanly, finish the
  episode;
- **dsh / pi** (external CLI): each phase boots a **fresh container** and hands the
  CLI built from the run's current source that phase's prompt as its task; the workspace
  mounts at `/workspace`, the
  harness's own state at `/state`, and an optional benchmark workspace at
  `/workspace/task`; self-edited code takes effect via the rebuild-on-boot wrapper.
  DSH and Pi also bind `<run>/.proteus-state` over `/workspace/.proteus`: it remains
  writable under workspace-only permissions while staying outside the measured harness.
  Before each phase, the prior handoff is exposed; after it, an agent-written operational
  summary is archived. A budget or timeout stop falls back to normalized tool names and
  paths, never raw reasoning or tool results. History lives under
  `.proteus-state/handoffs/epNNN/`, and reflect carries into the next episode.
  `max_turns` is enforced in two layers, both harness-agnostic: **exactly
  between phases** (no new phase once the budget is spent) and **approximately
  mid-phase** (the session log is polled live — pi's is plain JSONL, dsh's flushes one
  zstd frame per event — and the container is stopped when the count crosses the
  budget). `min_turns_per_phase` additionally reserves turns for later phases: while
  phase i runs, its stop line is `max_turns - min_turns_per_phase x phases_after_i`, and
  reaching the line ends the phase, not the episode — so a greedy observe cannot starve
  act. A budget stop records `turn_capped`, not an error: files already written
  persist, the episode snapshots normally, the run continues. `phase_timeout_s` remains
  the wall-clock backstop. With `announce_budget`, the agent is also *told* its budget
  in every phase prompt, so it can plan within it — off by default, because announcing
  changes behaviour, and recorded in the manifest;
- **aki**: delegates the episode to Aki's own supervisor.

An exception or `res.ok == False` is recorded and ends the trajectory — a record, not a
crash.

### 4. Read the trace — adapter

`adapter.read_trace(root, ep)` parses the harness's **own logs** into normalized
`ActionEvent`s (turn / phase / tool / surface / params / text). This is the only channel
Proteus reads behaviour through — never the agent's self-report, and never by
instrumenting the harness. Path→surface attribution is the adapter's mapping
(`src/…` → `loop`, etc.).

### 5. Run every evaluator — framework

`cfg.goal.evaluate(trace, ctx)` runs all evaluators **before the snapshot** (so
selection can still reject the episode):

- each evaluator carries its own kind (measurement / benchmark / custom) and visibility
  (HIDDEN / OBSERVE);
- an evaluator is user code — **its crash must not take the trajectory down**; it
  degrades to a scored zero and the run continues;
- the timing contract: between one episode's end and the next one's start, every result
  is complete.

### 6. Selection — framework

Under `selection="accept_reject"`: mean score below the best so far → reject. Selection
reads scores directly and is independent of visibility — an outer loop may act on scores
the agent itself never sees.

### 7. Snapshot — framework (the rejection semantics matter)

- **accepted**: commit `episode N` (`--allow-empty` — an episode that changed nothing
  still maps to exactly one commit; the episode→commit mapping must have no gaps);
- **rejected** (non-destructive):
  1. commit `candidate N [rejected]` first — the rejected tree **enters history**, the
     evidence is kept;
  2. `git restore --source` back to the last accepted state (not `reset --hard`, which
     would orphan the candidate commit);
  3. `clean -fdx` — ignored files go too, or the rejected episode's residue leaks into
     the next one;
  4. commit `episode N [rejected]`, keeping the mapping gapless.

Only `harness/` participates in selection. A benchmark task is the exercise rather than
the measured subject, so `<run>/task/` moves forward and is not restored when a harness
candidate is rejected.

### 8. Records and feedback — framework

- `eval_history` appends every result plus the accept/reject flag;
- numeric counters (`tokens_in` / `tokens_out`, …) sum across episodes into
  `RunResult.counters`;
- `prior_feedback` becomes the OBSERVE-visible feedback text for the next episode's
  step 1 (with a "your changes were not kept" note after a rejection);
- the progress line — which carries the condition label and **HIDDEN scores** — goes to
  `progress_path`, which **must live outside the run root**: the subject can read its
  own run root.

### 9. Next episode — framework

Context-fresh: the next episode wakes up to the working tree the snapshot describes. In a
framework-continuity run, the prior reflect's bounded operational handoff also crosses the
boundary as apparatus state; because it lives outside the harness snapshot, it cannot
count as evolved memory. Raw conversation and process state never survive.

---

## Shared by every harness

| part | where |
|---|---|
| phase-prompt assembly rules (where goal / feedback / disposition inject) | `episode._phase_prompts` |
| framework continuity protocol, redaction, phase history, fallback | `continuity.py` |
| evaluator timing, visibility, crash degradation, selection | `goal.py` + `episode.run` |
| snapshots, non-destructive rejection, gapless mapping, no ignore rules | `snapshot.py` |
| records: eval_history / counters / progress lines | `episode.run` |
| resume, counted in snapshot commits | `episode.completed_episodes` |
| the measurement suite — structural distance, travel, behavioural R, audit, reliability — reads the same artefacts for every harness | `proteus/measure/` |
| container infrastructure (image / network / mounts / resources, all user-configurable) | `proteus/sandbox/` |

## Owned by the adapter (seven methods, optional capability attributes)

| contract item | decides | examples of divergence |
|---|---|---|
| `name` | stable adapter identity used in records and diagnostics | minimal / dsh / pi / aki |
| `continuity_mode` | native / framework / none; absent means native | dsh/pi: framework; aki: native; minimal: none |
| `surfaces()` | the measurable surface manifest (data, not a constant) | minimal: notes+tools; dsh: +instructions+loop; pi: +skills |
| `seed()` | the episode-0 state | dsh/pi extract their own code into `src/` |
| `install_disposition()` | the perturbation's carrier | minimal: a JSON file; dsh/pi: a marked `AGENTS.md` block |
| `disposition_fingerprint()` | F-drift detection | hash of the block / file |
| `run_episode()` | how the four phases execute | in-process / one container per phase / delegated |
| `read_trace()` | each harness's log format → `ActionEvent`s | JSONL / zstd JSONL / session events |
| `required_edit_tools()` | evidence the harness can still edit itself | write / write+edit |
| `disposition_in_files` | skip the prompt channel (double-dose guard) | True for dsh/pi |
| the self-code arrangement | how the harness's own code becomes an evolvable surface | aki: `sys.path`-first copy; dsh: shadow-mounted bundle; pi: rebuilt from real source at boot |

## The built-in harnesses, side by side

| | minimal | llm | dsh | pi | aki |
|---|---|---|---|---|---|
| execution | in-process, mock policy | in-process, live model | container per phase | container per phase | delegated supervisor |
| trace source | own JSONL | own JSONL | `session.jsonl.zstd` | session JSONL | Aki tracer |
| disposition carrier | JSON file | JSON file | `AGENTS.md` block | `AGENTS.md` block | apparatus-native |
| continuity | none | none | Proteus framework handoff | Proteus framework handoff | native supervisor |
| self-code | none | none | bundled ESM (shadow mounts) | **real TS source (rebuild on boot)** | `loop.py` + package copy |
| iteration bound | `max_turns`, hard | `max_turns`, hard | `max_turns`: exact between phases + mid-phase log watch | `max_turns`: exact between phases + mid-phase log watch | apparatus turn gate |
| needs | nothing | API key | Docker + key | Docker + key | the private Aki repo |

## Failure paths

| situation | outcome |
|---|---|
| a phase times out / the CLI exits nonzero | episode records the error; trajectory ends; completed episodes all kept |
| the agent breaks its own code | the boot gate catches it (for pi, including compile errors) — no API spend, legible error |
| an evaluator crashes | scored zero, run continues |
| the episode is rejected by selection | candidate tree preserved in history, working tree rolled back, mapping gapless |
| the process is killed mid-run | `--on-existing resume` continues after the last snapshot commit — finished episodes are never paid for twice |
