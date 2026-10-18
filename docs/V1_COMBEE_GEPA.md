# Fixed-batch Combee GEPA method

`proteus.combee_gepa@1` composes the [recorded Combee operator](V1_COMBEE_AGGREGATION.md)
with the [managed GEPA method](V1_GEPA_METHOD.md). It runs seed validation, parent
selection, training minibatches, grouped reflection, candidate evaluation,
acceptance, full validation, final selection and optional hidden tests. Each
accepted or rejected proposal retains the ordinary candidate lineage and audit
records. An invalid reflection output produces no candidate; an infrastructure
failure stops the method without inventing scores or an unrecorded retry.

## Scope and pins

This is an **adapted fixed-batch profile**. The engine remains at GEPA
`8b0ce6cd99a234f6b74daf37558a2ac0ce18f975`; the reflection operator is the separately
pinned ComBEE implementation `f1cfdee2856e9dc4b5b4dc5cc2e391d0aa20d819`. The engine
pin is in `reference`; `operator_revision` and the bundled implementation hashes
lock the operator. A reference-profile claim is rejected at configuration time.
The recipe must state its deviations.

The current engine profile is single mutation, full validation, instance
frontier, no merge and no evaluation cache. Formal task evaluation is serial;
the reflection map is concurrent when `batch_reflection=true`. This does not
implement the paper's complete parallel evaluation path, dynamic batch sizing,
profiling or Combee ACE training. It does not establish quality or speedup.

## Configuration and extension

Register `proteus.methods.combee_gepa:register`. All ordinary `GepaOptions`
remain available, including a reflective-dataset stage, custom search policies,
component selection through the engine, and final test evaluations. Additional
options are:

| Option | Default | Meaning |
| --- | --- | --- |
| `minibatch_size` | 9 | Fixed original task occurrences per reflection minibatch |
| `duplication_factor` | 2 | Reflection copies before grouping; does not increase task counts |
| `max_workers` | 4 | Map worker ceiling, also bounded by run and role limits |
| `batch_reflection` | true | Concurrent managed map calls; false uses ordered serial calls |
| `aggregation_prompt_template` | null | Optional template with `<curr_param>` and `<side_info>` |
| `operator_revision` | full `f1cfdee…` pin | Required operator identity; other revisions are rejected |

The prompt stage must declare parallel and replay capabilities when maps are
enabled. Its actor must support fresh parallel calls. Parent selection,
minibatch sampling and grouping share the framework RNG. Recorded grouping and
wave operations are keyed by iteration, preserving the same random sequence
and individual calls across mid-iteration recovery.

The run budget still includes seed/parent/child/validation/hidden evaluations.
GEPA checks its metric threshold between iterations; use `evaluation_bound`
plus hidden-test tasks for an evaluation hard cap. Reflection counts in the
GEPA summary include every map and reduce completion. Cost and tokens come
from provider-reported usage in the shared ledger; missing cost remains unknown.

## Completion metadata

`ActorResult.finish_reason` is optional and preserved in operation records and
recovery. The chat transport reads the provider's selected choice. Combee passes
this metadata through whitespace trimming into the reference parser. Unfenced
outputs with `length` or `max_tokens` are rejected, while a complete fenced
proposal retains the reference parser's acceptance rule. Without metadata, the
parser keeps its documented text-based salvage behavior. Custom actors should
report the reason when the provider supplies it; Proteus does not infer one.

## Offline end-to-end example

Install Proteus and the pinned GEPA optional dependency, then run:

```sh
python -m proteus.examples.combee_gepa --out /tmp/combee-gepa-study
proteus recipe lock /tmp/combee-gepa-study/recipe.json \
  --plugin proteus.examples.combee_gepa:register \
  --source "proteus=/path/to/Proteus" --out /tmp/combee-gepa-study/lock.json
proteus recipe run /tmp/combee-gepa-study/recipe.json \
  --plugin proteus.examples.combee_gepa:register \
  --lock /tmp/combee-gepa-study/lock.json \
  --artifact-store /tmp/combee-gepa-study/artifact-store --out /tmp/combee-gepa-study/run
proteus recipe resume /tmp/combee-gepa-study/recipe.json \
  --plugin proteus.examples.combee_gepa:register \
  --lock /tmp/combee-gepa-study/lock.json --out /tmp/combee-gepa-study/run
```

The synthetic fixture uses nine training tasks and two tasks each for validation
and hidden testing. One successful mutation uses 22 optimization evaluations,
two hidden evaluations and four reflection invocations: three map calls and one
reducer. All 28 actor invocations are explicitly model-free. A completed resume
does not repeat them. This example validates the wiring; real model experiments
need their own data, actors, budgets and declared study protocol.
