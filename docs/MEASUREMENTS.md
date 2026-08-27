# Adding a measurement

A measurement answers a question about **how a harness evolved**. It is not necessarily a
task the agent must solve, and it does not have to be shown to the agent. Proteus has two
measurement extension points because measurements that need one episode are structurally
different from measurements that compare checkpoints, seeds, or experimental arms.

## Choose the right extension point

| You need to measure | Extension point | When it runs | Automatic outputs |
|---|---|---|---|
| the current episode's trace or working tree | per-episode `EvaluatorSpec(kind="measurement")` | after the episode and trace, before selection and snapshot | private `.proteus-records/<run-id>/eval_history.json`; in a sweep, progress JSONL and live report; optional next-episode feedback |
| checkpoints, path length, multiple seeds, or multiple arms | post-run module/function | after a run or sweep | none; return or write your own structured result, or extend `proteus measure` |
| provider usage or adapter-native counts | `EpisodeResult.counters` | reported by the adapter with each episode | per-episode progress JSONL; cumulative `RunResult.counters` and `seeds.jsonl` |

Use a benchmark instead when the measurement requires seeding an exercise and grading its
solution. See [`BENCHMARKS.md`](BENCHMARKS.md). A measurement observes the evolving harness;
a benchmark supplies external task ground truth.

## The artifacts a measurement may read

For a sweep rooted at `runs/demo/`:

```text
runs/demo/
├── manifest.json                  # plan + immutable, versioned experimental condition
├── seeds.jsonl                    # one completion record per run attempt
├── progress/<run-id>.jsonl        # live per-episode units, scores, counters
└── runs/
    ├── .proteus-records/<run-id>/ # framework-private; outside the subject run root
    │   ├── eval_history.json      # evaluator results and accept/reject decisions
    │   ├── disposition_fingerprint.json
    │   └── pending_candidate.json # present only while a staged failure awaits repair
    └── <run-id>/
        ├── harness/               # current measured harness state
        ├── task/                  # optional benchmark; not measured/snapshotted
        ├── .snapshot.git/         # episode 0 + one mapped commit per completed episode
        └── traces/                # adapter-owned trace references or normalized logs
```

Activation safety is not a measurement evaluator. The three Phase 1 families live on
the candidate gate: `memory_bad_admission`, `memory_collapse`, and
`tools_permission_drift`. Bad admission scores module keep vs episode follow on an
AdvBench sample. Collapse occupancy-probes a snapshot copy on selected episodes and
does not decide activation. Permission scores protected vs allowed canaries; committing
a protected fixture is a fail. Reports come from `proteus safety harness-report`.

Treat `harness/` and its snapshot chain as the measured subject. `task/`, provider session
state, build caches, condition labels, and hidden scores are apparatus or experiment
records; do not silently count them as harness structure. Hidden evaluator history is
outside `<run-id>/`, so even an adapter that exposes the whole run root cannot reveal it.

## Path A: a per-episode measurement evaluator

The callable contract is:

```python
def measurement(
    trace: Sequence[ActionEvent], ctx: GoalContext
) -> EvalResult: ...
```

`ctx.harness_root` is the current `<run>/harness/`, and `ctx.episode` is one-based. The
callable runs after `adapter.read_trace(...)` and before selection/snapshotting. It must be
read-only: a measurement that writes into the harness changes the candidate tree it claims
to observe.

### Complete example

This intrinsic measurement scores how many durable Markdown headings the harness has added
to its notes surface:

```python
from pathlib import Path
from typing import Sequence

from proteus.core import (
    ActionEvent,
    EvalResult,
    EvaluatorSpec,
    GoalConfig,
    GoalContext,
    Visibility,
)


def note_headings(
    trace: Sequence[ActionEvent], ctx: GoalContext
) -> EvalResult:
    notes = Path(ctx.harness_root) / "notes"
    count = 0
    if notes.is_dir():
        for path in notes.rglob("*.md"):
            try:
                count += sum(
                    line.lstrip().startswith("#")
                    for line in path.read_text(
                        encoding="utf-8", errors="replace"
                    ).splitlines()
                )
            except OSError:
                continue
    return EvalResult(
        name="note-headings",
        score=float(count),
        passed=count >= 10,
        detail=f"{count} Markdown headings under notes/",
    )


measurement = EvaluatorSpec(
    name="note-headings",              # stable identity in records/report
    run=note_headings,
    kind="measurement",
    visibility=Visibility.HIDDEN,       # or OBSERVE for next-episode feedback
)

goal = GoalConfig.of(
    text="Make your durable knowledge easier to navigate.",
    evaluators=(measurement,),
)
```

Pass `goal` to `RunConfig` or `SweepConfig`. Goal text and measurement are independent: an
empty `text` gives a measured no-goal condition; `Visibility.OBSERVE` shows the last result
in the next episode's observe prompt; `HIDDEN` records it without showing the agent.

### Result contract

`EvalResult` has four fields:

| Field | Meaning |
|---|---|
| `name` | result label; `EvaluatorSpec.name` is the canonical identity and overrides a different returned name |
| `score` | numeric value used by reports and optional selection; higher is better when `accept_reject` is enabled |
| `passed` | optional threshold verdict; stored for analysis, not used by selection |
| `detail` | short human-readable explanation stored with the result |

Scores do not have to lie in `[0, 1]`; built-in unit and activity measurements are counts.
However, `accept_reject` takes the unweighted mean of every attached evaluator score. Do
not mix incompatible scales under selection without normalizing them first.

An uncaught evaluator exception is converted into an `evaluator-error` result with score
zero and the trajectory continues. Keep the callable deterministic, bounded, read-only,
and exception-safe so one broken measurement does not replace that episode's result set.

### Built-in examples

[`proteus/core/evaluators.py`](../proteus/core/evaluators.py) contains the reference
implementations:

- `surface_units(surface)` — current unit count on one declared surface;
- `tool_calls()` — activity from the normalized action trace;
- `structural_step(surfaces)` — movement from the previous snapshot to the current tree.

The CLI exposes those as `units:<surface-name>`, `tool-calls`, and `step`. The unit form
takes the name declared on `Surface`, resolves its file or directory through `subdir`, and
counts according to its declared `unit` (`file`, `directory`, or `top_level_def`). This
includes single-file surfaces such as `AGENTS.md`. A declared subdirectory is also
accepted for compatibility with early v0.1 examples, but run records use the canonical
surface name. Every form also accepts `@hidden` (default) or `@observe`:

```bash
proteus run --harness pi \
    --goal "Make your durable knowledge easier to navigate." \
    --evaluator units:notes@observe \
    --evaluator step \
    --evaluator tool-calls \
    --arm neutral --seeds 2 --episodes 10 --out runs/navigation
```

There is currently no `--evaluator module:function` plugin loader. External measurements
use the Python API. To add a new first-party CLI form, add its factory to
`proteus/core/evaluators.py`, parse the form in `proteus.cli._evaluator`, update `run
--help`, and add an offline test in `tests/test_goals.py`.

## Path B: a post-run or sweep-level measurement

Use this path when the statistic needs past checkpoints, other seeds, condition labels, or
permutation/resampling. Such a function should read completed artifacts and return plain,
serializable data. It must not modify a run.

### Complete checkpoint example

This measurement compares episode 0 with the latest completed harness state and reports
per-surface structural churn:

```python
import tempfile
from pathlib import Path

from proteus.core import snapshot
from proteus.measure import distance


def endpoint_churn(run_root: Path, adapter) -> dict[str, dict]:
    run_root = Path(run_root)
    work = run_root / "harness"
    seed_sha = snapshot.commit_for_episode(work, 0)
    final_sha = snapshot.head(work)
    if seed_sha is None or not final_sha:
        return {}

    with tempfile.TemporaryDirectory() as tmp:
        seed = Path(tmp) / "seed"
        final = Path(tmp) / "final"
        snapshot.materialize(work, seed_sha, seed)
        snapshot.materialize(work, final_sha, final)
        deltas = distance.compare(seed, final, adapter.surfaces())

    return {
        name: {
            "added": delta.added,
            "dropped": delta.dropped,
            "revised": delta.revised,
            "distance": delta.distance,
            "churn": delta.churn,
        }
        for name, delta in deltas.items()
    }
```

To aggregate a sweep, read non-empty JSON lines from `seeds.jsonl`, group records by
`arm`, and call the run-level function on each `record["root"]`. Preserve the seed-level
values in the output; reporting only a mean makes reliability and uncertainty impossible
to inspect later.

### Built-in examples

The existing post-run modules show the main shapes:

| Module | Reads | Produces |
|---|---|---|
| `proteus.measure.distance` | declared surfaces and materialized snapshots | units, endpoint deltas, path length |
| `proteus.measure.stream` | normalized `ActionEvent` tool streams across seeds/arms | frequency/order/procedure distance, reliability, permutation-tested between/within ratio |
| `proteus.measure.crystallize` | checkpoint states re-mounted under a removable disposition | fidelity and arm-shift statistics |
| `proteus.measure.audit` | authored files, episode-0 baseline, trace text | quoted escape/awareness evidence rather than a scalar score |

`proteus measure`, `proteus reliability`, and `proteus audit` are explicit CLI consumers
of these modules. Post-run functions do not auto-register. To ship a new first-party CLI
measurement:

1. add a focused module or function under `proteus/measure/`;
2. return a dataclass or JSON-serializable dictionary with named fields;
3. call it from `cmd_measure` or add a dedicated subcommand when its inputs differ;
4. define behavior for missing/partial snapshots and insufficient seeds;
5. add regression tests for the statistic and a small end-to-end run;
6. document interpretation, direction, scale, minimum sample size, and failure modes.

The live report automatically renders only per-episode evaluator scores placed in progress
JSONL. A post-run statistic needs an explicit output file/schema and report UI change if it
should appear there.

## Statistical and implementation rules

1. **Measure declared surfaces, not hard-coded harness names.** Accept
   `adapter.surfaces()` or a `Sequence[Surface]` whenever the statistic concerns structure.
2. **Use normalized traces, not provider logs.** The adapter owns provider-specific parsing;
   measurements consume `ActionEvent`.
3. **Keep unit identity stable.** Use paths relative to the surface. Two same-named files in
   different directories are different units; a directory unit must hash every member.
4. **Treat broken intermediate code as data.** A structural measurement must degrade
   legibly when a source file does not parse, not crash or silently ignore it.
5. **Preserve episode mapping.** Resolve checkpoints with `snapshot.commit_for_episode`;
   never assume `HEAD~N` maps to episode N because rejected candidates also enter history.
6. **Separate reproducibility from separation.** Before comparing arms, establish that
   repeated seeds within each arm resemble one another (`proteus reliability`).
7. **State direction and scale.** Say whether larger is better, worse, or merely more, and
   whether the value is bounded. Do not call activity or structural churn quality.
8. **Fail explicitly on undefined statistics.** Too few arms/seeds or an empty action pool
   should raise a clear error or return a named unavailable state, never a plausible zero.
9. **Keep measurement read-only.** This is mandatory for per-episode evaluators because
   they run before the candidate snapshot.
10. **Avoid hidden network/model dependencies.** If a measure uses an LLM judge or external
    service, name the model/version, timeout, retry policy, cost, and caching behavior.

## Testing a contributed measurement

At minimum, add offline tests covering:

- a hand-computable positive case and a zero/empty case;
- determinism under a fixed seed;
- missing surfaces, partial runs, and malformed source/trace input;
- stable output names and JSON-serializable values;
- visibility routing for a per-episode evaluator;
- no mutation of `harness/`;
- the minimum valid number of seeds/arms for a group statistic;
- a regression test for every previously discovered edge case.

A minimal evaluator test looks like:

```python
from proteus.core import GoalContext


def test_note_headings(tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "index.md").write_text("# One\n\n## Two\n", encoding="utf-8")

    result = note_headings([], GoalContext(str(tmp_path), 1))

    assert result.name == "note-headings"
    assert result.score == 2.0
    assert not result.passed
```

Run the full offline suite before contributing:

```bash
python3 -m pip install -e '.[dev]'
ruff check .
pytest tests/ -q
python tests/run_offline.py
```

## Contribution checklist

- [ ] correct extension point chosen: per-episode evaluator, post-run statistic, or counter
- [ ] callable is deterministic, bounded in runtime, read-only, and exception-safe
- [ ] harness structure comes from declared surfaces; behavior comes from `ActionEvent`
- [ ] output fields, direction, scale, interpretation, and sample requirements documented
- [ ] partial/empty/undefined cases are explicit
- [ ] per-seed observations retained before aggregation
- [ ] CLI parser/help and report schema updated only when first-party integration is intended
- [ ] offline unit and end-to-end tests pass
