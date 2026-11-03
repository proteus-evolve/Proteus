# Recorded Combee aggregation operators

P08b adds reusable ACE and GEPA aggregation operators on top of the P08a
stage-map scheduler. A method owns generation, evaluation, counter updates,
candidate acceptance and batch rollback. The operators own reflection grouping,
managed proposal/reducer calls and the resulting update proposal.

This is an aggregation milestone. A fixed-batch GEPA integration is described in
[V1_COMBEE_GEPA.md](V1_COMBEE_GEPA.md). Complete `combee_ace` and parallel GEPA
experimental recipes, concurrent task evaluation, per-sample adaptive loops,
profiling, dynamic batch control and quality/cost/elapsed-time experiments remain
in P08. Running these offline examples does not establish model performance or
parallel speedup.

## Source-specific semantics

| Rule | ACE | GEPA |
| --- | --- | --- |
| Reference commit | `82709de050e1db6e6ef2f07bcb0393560b94992a` | ComBEE `f1cfdee2856e9dc4b5b4dc5cc2e391d0aa20d819` |
| Before shuffle | Each record repeated p times contiguously | Entire record list repeated p times |
| Shuffle at p=1 | No | Yes when n≥4 |
| Default groups | floor(sqrt(original n)) | floor(sqrt(original n)) |
| n<4 | Map groups and reduce any ADD proposals | One ordinary reflection call; no duplication/shuffle |
| One surviving proposal | Still reduce | Use directly |
| Update | ADD operations; apply final delta once | Synthesized text components; method decides acceptance |

Group size remainder goes to the earlier groups. GroupPlan stores original
DataRefs, source/copy occurrence indexes, grouping options and complete RNG
states. Same-content records are separate sample occurrences; augmentation does
not create extra tasks or evaluations. Plans are journaled before dispatch, so
recovery neither reshuffles nor advances the shared method RNG again.

ACE map calls all read the same already-counter-updated playbook. The exact
reference curator prompt/parser is used; proposals are filtered to ADD without
applying them first. Nonempty groups are reduced in group order, with original
sample numbers. The caller applies the returned `PlaybookDelta` once. Invalid
model JSON and the empty-response sentinel produce a recorded skipped proposal.
Infrastructure failures stop aggregation instead of being scored as zero.

GEPA runs three vendored upstream modules with only import relocation. Its base
optimizer dependency remains at the existing separately verified pin. Input
mapping order is preserved through `ReflectionBatch`, including after recovery.
The upstream incomplete-output parser and multi-component wave scheduling are
retained. Plain unfenced text and partial code fences follow the reference's
permissive salvage behavior; an unclosed leading reasoning block is rejected.
ActorResult also preserves optional provider `finish_reason` metadata. The
managed wrapper carries it through trimming into the reference parser: known
truncated unfenced outputs are rejected, while complete fences retain upstream
acceptance semantics. Transport errors propagate rather than triggering an
unaccounted upstream fallback.

## Method API

Register `proteus.operators.combee:register` in addition to the method's codecs
and stages. The method must enable `run.journal` and `run.resume`. Its prompt
stage takes `proteus.text@1` and returns `proteus.actor_result@1`; parallel stages
and actor runtimes must declare the P08a capabilities.

```python
from proteus.operators.combee.ace import aggregate_ace
from proteus.operators.combee.gepa import aggregate_gepa

ace_result_ref = aggregate_ace(
    services, counter_updated_artifact, reflection_record_refs,
    stage="curator", operation_id="batch-3/curation", max_workers=4,
    step=32, total_samples=250,
)
delta = services.values.get(ace_result_ref).delta

gepa_result_ref = aggregate_gepa(
    services, candidate_texts, ordered_feedback_records, components_to_update,
    stage="reflection", operation_id="iteration-3/reflection", max_workers=4,
    duplication_factor=2,
)
updates = services.values.get(gepa_result_ref).new_texts
```

ACE record refs contain `AceRecord(sample_number, reflection, context)` values.
GEPA feedback is a mapping from component names to ordered JSON record lists;
multimodal records are not supported by this managed text profile. Operator
results retain all plan and wave refs, including raw responses for discarded
proposals. Every wave uses a distinct operation occurrence: equal prompts are
not a cross-iteration response cache.

Run and role concurrency limits still bound dispatch. Interrupted maps preserve
their timing limitation from P08a; this operator does not infer full-iteration
elapsed time from an aggregation wave. Actual usage comes from the Proteus
ledger. Upstream TrackingLM token estimates and zero-dollar placeholders are
not imported as measured usage or used to enforce a spend cap.

## Installed examples

The ACE example requires only the Proteus package:

```sh
python -m proteus.examples.combee_aggregation --family ace --out /tmp/combee-ace-demo
proteus recipe lock /tmp/combee-ace-demo/recipe.json \
  --plugin proteus.examples.combee_aggregation:register \
  --source "proteus=/path/to/Proteus" --out /tmp/combee-ace-demo/lock.json
proteus recipe run /tmp/combee-ace-demo/recipe.json \
  --plugin proteus.examples.combee_aggregation:register \
  --lock /tmp/combee-ace-demo/lock.json \
  --artifact-store /tmp/combee-ace-demo/artifact-store --out /tmp/combee-ace-demo/run
proteus recipe resume /tmp/combee-ace-demo/recipe.json \
  --plugin proteus.examples.combee_aggregation:register \
  --lock /tmp/combee-ace-demo/lock.json --out /tmp/combee-ace-demo/run
```

Use a new destination with `--family gepa` for the GEPA example. First install
the optional engine reference:

```sh
python -m pip install --no-deps 'gepa @ git+https://github.com/gepa-ai/gepa.git@8b0ce6cd99a234f6b74daf37558a2ac0ce18f975'
```

The GEPA example has no initial artifact store, so omit `--artifact-store`.
Both examples use an explicitly offline actor and report zero model calls.
The ACE fixture carries a playbook subject declaration to lock its codec; it
does not run task generation or evaluation.
