# Recorded dynamic batches for ACE and GEPA (P08g)

`DynamicBatchOptions` enables a shared Combee throughput controller in
`proteus.combee_ace`, `proteus.combee_gepa` and the underlying GEPA method.
The controller follows [Combee v2, section 3.3](https://arxiv.org/html/2604.04247v2#S3.SS3):
ordinary learning iterations profile candidate batch sizes, their durations become
epoch-time estimates, and a power-law fit selects a bounded plateau. Profiling
is part of training and uses the same evaluations, managed calls and budgets.

## Configuration

Set the method's `dynamic_batch` option:

```json
{
  "max_batch_size": 64,
  "min_batch_size": 1,
  "profile_batch_sizes": [1, 2, 4, 8, 16],
  "slope_threshold": 0.016
}
```

Omitting the option preserves fixed batching. When it is present, the controller
chooses batch sizes instead of the fixed `batch_size`/`minibatch_size` field.
Without explicit profiling sizes, sizes double from the minimum through the
maximum. Sizes are capped by the dataset; ACE online also caps them by its window.
Flat or increasing fitted epoch-time curves, or fewer than two usable observations,
retain the smallest profile size. No quality optimum is inferred from timings.

The controller is a reusable module in `proteus.operators.combee.batching`.
It receives recorded measurements and returns serializable state; it does not
dispatch workers or select tasks. Methods retain their own order and feedback rules.
ACE processes its declared samples and never crosses an online scoring window.
GEPA preserves the unvisited tail of its shuffled epoch when size changes, and
uses least-frequency padding without repeating a task within one minibatch.
The current GEPA profile remains single-proposal, no merge and no evaluation cache.

## Timing and recovery

`RunServices.begin_timing()` and `finish_timing()` journal separate start/end
operations around managed work. The measurement records a continuous monotonic
duration only when both endpoints belong to the same invocation. A completed
measurement replays exactly; an interrupted interval whose end was not committed
has unknown duration. Neither downtime nor fast replay is used to estimate speed.

ACE measures the complete three-phase training batch, excluding baseline, validation
and final tests. GEPA measures sampling through the iteration-end callback,
including parent/child evaluation, reflection and any candidate validation; it
excludes the next iteration's parent selection and engine checkpoint write.
These scopes are explicit adaptations, not a claim of identical paper timing.

Each profiling observation records the requested/actual size, measurement reference,
elapsed duration, epoch estimate and any exclusion reason. ACE short final batches
still train and are charged, but do not profile a larger requested size. Unknown
and zero durations are retained as unusable observations. Once the profile ends,
its selected size is fixed for the rest of the run. Checkpoints preserve that
decision, sampler cursor, ordered frequencies and RNG state.
On recovery, each observation is checked against its original immutable timing
record, and its exclusion reason and epoch estimate are recomputed. A checkpoint
cannot silently alter a measured duration or discard a usable sample.

The paper's original profiling data and environment are not reproduced by a
fixture. The implementation is compared against the actual curve and sampling
code at [GEPA proposal `f9e2d43`](https://github.com/gepa-ai/gepa/pull/492), which
was open and unmerged when checked on 2026-10-18. It is a pinned conformance source,
not a released runtime dependency; the existing GEPA engine pin is unchanged.

## Deterministic transport examples

```sh
python -m proteus.examples.combee_ace --out /tmp/combee-ace-dynamic \
  --workers 3 --dynamic-max-batch-size 4
python -m proteus.examples.combee_gepa --out /tmp/combee-gepa-dynamic \
  --dynamic-max-batch-size 4
```

Use the lock/run/resume commands from the corresponding
[ACE](V1_COMBEE_ACE.md) and [GEPA](V1_COMBEE_GEPA.md) recipe documentation.
These fixtures make no inference calls. Their measured times exercise the mechanism
and must not be used to claim a model-serving speedup or published quality result.
The real-domain experiment package, same-budget baselines and model experiments
remain part of the 1.0 acceptance plan.

CI checks the pinned source's curve and sampling order, full ACE/GEPA loops,
hard exits at both timing boundaries, and finished resume without new calls.
Both wheel and sdist run the dynamic examples through Python and the public CLI
outside the checkout. The online ACE fixture also checks that a short batch at
a scoring-window boundary stays excluded from fitting while its learning work
remains recorded.
