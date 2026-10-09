# Proteus Visualize

An installed, local workspace for real Proteus sweeps: looping evolution replay,
per-episode evidence, measurements, configuration drafts, and snapshot identities.
It uses the same CLI and measurement library as the rest of Proteus, not a second
evolution engine. No frontend build, Node installation, or model API call is needed to view.

## Open existing runs

```bash
proteus visualize --out runs/demo
# http://127.0.0.1:8301
```

`--out` accepts one sweep (a directory with `manifest.json`) or a directory whose
immediate children are sweeps. A run is identified by an opaque ID, not a client-supplied
filesystem path. Completed episodes are counted from contiguous snapshot commits;
trace files alone do not establish completion. The workspace refreshes every ten
seconds while idle, and never declares a controller alive merely because a PID file exists.

Try the complete offline path first:

```bash
proteus run --harness minimal --arm neutral --arm review:notes \
  --episodes 8 --seeds 2 --evaluator tool-calls@observe \
  --eval-initial tool-calls --out runs/visualize-demo
proteus visualize --out runs
```

The offline harness is a mock policy, not a claim about a live agent's performance.
Its displayed commits and measurements are nevertheless actual run evidence.

## Four views

- **Evolution:** named evaluator score, structural displacement, or tool-frequency
  divergence; episode or cumulative normalized tool actions on the horizontal axis;
  looping replay, surface changes, and comparison with another recorded run. Comparisons
  do not establish experimental comparability: choose matching goals, evaluator definitions,
  models, and conditions. Different scales and missing observations are not averaged.
- **Measurements:** structural displacement, valid-checkpoint path length, candidate
  revision/removal fraction, tool-frequency/bigram Jensen–Shannon divergence, and
  compression distance. **Measure matched seeds** computes within-arm reliability and
  between-arm separation at the selected run's completed episode. Incomplete/missing
  endpoints are excluded explicitly. Separation is withheld unless every compared arm has
  two matched replicates and passes the within-arm reliability prerequisite. Parameters
  and provenance are displayed. These are descriptive instruments, not proof of improvement.
- **Configuration:** recorded goal, adapter/model, budgets, phases, evaluator visibility,
  schedules, selection and environment. A separate composer validates drafts against the
  core budget/goal protocols and generates a real `proteus run` command. Validation
  does not download benchmarks, import custom adapters, check credentials, or make calls.
- **Snapshots:** the valid runtime checkpoint and candidate commit are distinct. Failed
  candidates are visible without implying activation; selection-unavailable outcomes and
  viability failures are distinguished. Detail uses independent left/right scroll regions,
  with the goal collapsed by default.

“External evaluator score” means the score from a supplied evaluator, **not** a competing
method's baseline or H0. The selector lists every named instrument, including its kind and
visibility. An error-status legacy zero is displayed as missing; a valid zero remains zero.
No harness-authored evaluator or goal-completion verdict is inferred from a filename.

## Evidence and missing measurements

The viewer reads manifest declarations, private evaluation history, progress, and the
immutable snapshot repository. It does not import the run's adapter type or execute
the evolving subject. Structural units use the adapter's declared surfaces and the
public `proteus.measure.distance` implementation. Symlinks and unsafe archive paths
are not extracted or followed. Snapshot reads are bounded by size and time.

New progress records include `action_metadata`: only `turn`, `phase`, `tool`, and
`surface`, with no model text or tool parameters. This is harness-neutral and does not
change episode prompts, evaluator feedback, selection, or activation. Legacy normalized
JSONL traces are supported, but native session-map JSON files are **not** normalized
traces. Behavioral instruments remain missing for older native runs without this metadata.
No automatic parsing/backfill of private reasoning is performed.

Adapter-counted turns and normalized tool actions are different quantities. Numeric
adapter counters (including token usage when reported) are available in episode provenance.
Currency cost and elapsed time are unavailable unless recorded; this version does not
invent prices or turn counts. The USD axis therefore shows no evidence for existing
core runs that did not record monetary cost. Neutral-probe/crystallization measurements
require an explicit probe experiment; the viewer never launches one and leaves the
instrument unavailable. Use the Python measurement API for such experiments.

## Compose and launch

By default the server is read-only. Draft validation and command generation are allowed;
launches are disabled. Enable execution only as a trusted local operator:

```bash
proteus visualize --out runs --allow-run-control
```

When opening a single existing sweep, put new sweeps in a separate directory:

```bash
proteus visualize --out runs/demo --allow-run-control --launch-out runs/new
```

New sweeps appear in the catalog only when their destination is under the viewed directory.
The launch button confirms harness/model/episode/seed/arm counts and the possibility of
paid calls. It starts the existing CLI in a new, unique directory with `--on-existing refuse`.
It never overwrites, resumes, edits, or stops an existing experiment. Only one
viewer-launched sweep may be active per server. Controllers started elsewhere are not
claimed to be monitored processes. Closing the viewer **does not stop** its launched CLI
processes; inspect their private `controller.log` and existing Proteus run records.

Configure API credentials in the server process environment, following the adapter's
documented setup. **Never enter keys in the composer, goals, or evaluator specs.** Tokens
and credentials are not persisted in frontend drafts. Recognizable credential patterns are
rejected by configuration validation, but arbitrary secrets cannot be identified reliably.

The composer supports no goal/no evaluator, multiple instruments, cadence, H0 evaluation,
visibility, selection eligibility, explicit budgets, custom phases/prompts, arms and
container settings. Evaluators use existing CLI syntax (`tool-calls`, `step`, `units:NAME`,
`contains:PATH:TEXT`, and the supported benchmark prefixes); canonical surface names
are required. The CLI still supports one benchmark task workspace per run. Python API
custom evaluators can be viewed but are not imported or authored through HTTP.

The default offline configuration launches without additional setup. Its checkpoint
reserve is zero because `minimal` and `llm` do not carry operational continuity. A nonzero
checkpoint reserve for either harness is rejected during draft validation. Set a reserve
explicitly when using a harness with native or framework continuity, such as DSH, Pi, or Codex.

For a custom harness, explicitly trust its adapter for launching:

```bash
proteus visualize --out runs --allow-run-control \
  --allow-adapter mypackage.adapter:MyHarness
```

This allowlist authorizes Python code execution when you launch; it is not a sandbox.
The CLI performs adapter/environment checks at launch, and may fail even after the
draft's schema and protocol checks passed. Prepare images/dependencies beforehand.

## Local security and exports

The service binds only to `127.0.0.1`. Its asset allowlist does not serve manifests,
logs, source code, or arbitrary workspace paths. APIs require a per-server token, valid
local Host, and same-origin access; cross-site requests, CORS access and frame embedding
are rejected. There is no remote multi-user authentication or permission model.
For remote work use an authenticated SSH tunnel with the same local port:

```bash
ssh -L 8301:127.0.0.1:8301 user@experiment-host
# on that host: proteus visualize --out runs --port 8301
```

Do not expose the port publicly or use an unauthenticated reverse proxy. The local
viewer owner can inspect hidden scores; those results are never fed to the agent by
the viewer.

JSON export excludes hidden evaluator scores by default; including them requires an
explicit checkbox. Exports omit model reasoning, tool parameters, evaluator details,
and (by default) raw error diagnostics. Known credential forms are redacted recursively.
This is **not a guarantee of public anonymity**: goals, run names, file paths, tool names,
snapshot identities and observable outcomes may still be sensitive. Review them before
publishing. The JSON is an evidence export, not an executable manifest or source archive.
Export the source history separately with `proteus repo export` after a dedicated review.

## Development checks

```bash
pytest tests/test_visualize.py
node --test tests/visualize/model.test.mjs
python -m build
```

The static UI ships in both wheel and sdist. Node is only needed for frontend helper
tests, not for installation, viewing, or launch. No public website assets, research
campaigns, private traces, credentials, or promotion media are bundled.
