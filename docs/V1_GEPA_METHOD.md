# 1.0 development: executable GEPA method

P05c adds `proteus.gepa@1`: the pinned upstream optimization loop running through
Proteus actors, subjects, evaluation services, search policies and checkpoints.
The supported profile is single-mutation, round-robin text-component updates,
full validation, instance frontier, no merge and no evaluation cache. It includes
strict-improvement or improvement-or-equal acceptance and optional final hidden
test evaluations. The default upstream renderer, parser, acceptance and component
selector execute unchanged.

This completes a runnable method integration for that declared profile. It does
not complete R01/M2: a real benchmark with its complete declared split, model,
baseline and result analysis is still required. The included offline fixture
establishes mechanism/transport/recovery behavior; it is not a model experiment or
a reproduction of the paper's reported scores. Merge-enabled and other upstream
profiles need separate adapters and conformance evidence before support claims.

P05d adds the [AIME domain study](V1_GEPA_AIME.md), with a fixed complete dataset,
DSPy-format conformance, the reference mathematical metric/side information,
independent baseline/test arms and audited multi-seed reports. Its preparation
and HTTP fixtures do not satisfy the outstanding real benchmark requirement.

## Run the explicit offline fixture

Use the exact optional installation documented in [GEPA search](V1_GEPA_SEARCH.md).
Python package version alone is insufficient; the plugin checks the source bytes.
From the Proteus checkout, run:

```sh
python -m proteus.examples.gepa_prompt --out /tmp/gepa-study
proteus recipe lock /tmp/gepa-study/recipe.json \
  --plugin proteus.methods.gepa.method:register \
  --plugin proteus.examples.gepa_prompt:register \
  --source "proteus=$PWD" --out /tmp/gepa-study/lock.json
proteus recipe run /tmp/gepa-study/recipe.json \
  --plugin proteus.methods.gepa.method:register \
  --plugin proteus.examples.gepa_prompt:register \
  --lock /tmp/gepa-study/lock.json \
  --artifact-store /tmp/gepa-study/artifact-store --out /tmp/gepa-study/run
proteus recipe resume /tmp/gepa-study/recipe.json \
  --plugin proteus.methods.gepa.method:register \
  --plugin proteus.examples.gepa_prompt:register \
  --lock /tmp/gepa-study/lock.json --out /tmp/gepa-study/run
```

Method registration includes the search plugins; do not also register the separate
search hook. `example.gepa_offline` is a named deterministic fixture, reports zero
model calls and never sends a request to a model. It moves a single prompt from
`Return 0.` to `Return 1.` on synthetic data. The default run executes ten optimization
trials, one fixture reflection and two held-out test trials. Its evaluation hard
cap is nineteen; a cap is a limit, not a claim that all reserved work was executed.

The normal CLI result remains structured JSON. Upstream diagnostic text goes to
`run/private/gepa/engine.log`; journal replay may repeat diagnostic lines. Actual
invocations and charges come from the service records/ledger, not line counts.

## Recipe components and customization

`GepaOptions` names `train`, `validation`, `reflection_stage`, three independent
search slots and optional `test_evaluations`. It also declares `max_metric_calls`,
`minibatch_size`, `skip_perfect_score`, `perfect_score` and the acceptance rule.
Recipe seed supplies the shared parent/minibatch RNG. Training and validation must
be distinct observed, after-batch evaluation pools; the execution state is reset
for each task. Held-out test specifications remain hidden and selection-ineligible.
Declared task identities may not overlap across these pools. This check cannot
detect semantically duplicated examples assigned different dataset/split identities.

An adapted/extended profile can additionally set `dataset_stage` (typed
`proteus.gepa.reflection_batch@1` input `batch`, output `dataset`) and
`reflection_prompt_template` (with `<curr_param>` and `<side_info>` placeholders).
The stage receives only the already released training trajectory evidence. It
runs through the ordinary journal and preserves ordered records on replay. These
options are rejected by the unchanged generic reference profile.

- The subject loads the exact candidate artifact. Text components are the mutation
  surface. The builtin prompt subject supports one consumed component; custom
  subject plugins can consume multiple components. A multi-component builtin prompt
  configuration is rejected because silently optimizing unused text is invalid.
- The reflection stage consumes `proteus.text@1` and returns
  `proteus.actor_result@1`. Its request contains the actual upstream-rendered prompt
  with public training input, output, score, allowed feedback and recorded trace.
  This is a generic GEPA adapter's reflective dataset, not a claim of byte identity
  with every domain-specific adapter in the GEPA repository.
- A live configuration uses ordinary ActorSpec roles, for example `proteus.chat`
  with an explicit `base_url`, credential environment-variable name, model and
  settings. Optimizer and subject can use different actors. Replace the fixture
  roles and task dataset, pin the resulting configuration and run in a new directory.
  The synthetic fixture remains a wiring check even if live actors are substituted.
- The reference search slots use `proteus.gepa_parent`, `proteus.gepa_frontier` and
  `proteus.gepa_best`. The final policy uses the reference's sum/length and first-tie
  behavior. Replacing policies or the reflection stage requires an `adapted` or
  `extended` profile, preserving the actual plugin identity in the lock.
  For an adapted archive/parent pair, the parent policy receives only retained
  candidates, in accepted-program order; an empty retained population stops the
  method. The exact reference pair preserves original program indices because
  the upstream parent sampler internally applies the same frontier pruning;
  its selected parent must still belong to the recorded retained population.
  Retention constrains future parent selection, never erases candidate history,
  and does not restrict the separately configured final selector.

Each Method defines which stages it consumes. Unused stages/search slots/evaluations
are rejected, so adding an arbitrary stage to a recipe does not silently make it
part of the algorithm. A new control-flow mechanism belongs in a method plugin;
the shared services, policies, task sampler and state contracts remain reusable.
The first integration keeps the declared reference profile explicit rather than
accepting upstream options whose lineage or recovery semantics are not yet mapped.

## Candidate identity and outcomes

The bridge records every proposed candidate before its minibatch evaluation.
Accepted upstream program indices map to ordered Proteus candidate IDs. A copied
dictionary with identical text is still a new proposal and can be separately
accepted or rejected; content hashes never collapse its lineage. Parent/child
object and phase checks bind each upstream evaluation to its recorded candidate.
Full validation is checked against the exact declared validation pool/protocol.
At iteration boundaries, upstream parent indices and validation scores must match
the committed candidate lineage and evaluation evidence. Test evaluation IDs may
not shadow the reserved `summary`, `candidate` or `archive` output names.

`proteus.gepa.proposal@1` records accepted/rejected decisions and parent IDs.
`proteus.gepa.summary@1` reports accepted IDs, upstream best and configured final
selection, optimization metric calls, completed reflection invocations, iterations
and the stopping bound. Every proposal stays in the archive. Final hidden test
receipts disclose rollout identity without exposing private grades to the method.

Missing or infrastructure-error scores cause failure; they never become invented
zero scores. Authoritative stage/budget/integrity/recovery errors escape upstream
observational exception handlers. This bridge's declared failure policy stops on
such errors instead of allowing upstream reflection fallback to silently retry or
skip them. Successful-run algorithm conformance does not imply identical error
recovery to an uninstrumented upstream script. Requested actor retries belong to
an explicitly configured stage/profile and remain charged in the ledger.

## Budget boundary

GEPA checks `max_metric_calls` between iterations. For validation size `V`, minibatch
size `B`, and threshold `M`, the supported profile can use at most:

```text
V                         if M <= V
M - 1 + 2*B + V           otherwise
```

The final iteration may cross the threshold. This is the upstream stopping
semantics, not an evaluation-service overrun. A finite Proteus evaluation cap must
cover that bound plus all declared final test tasks; an undersized cap fails before
execution. Actual charges count actual task occurrences, including padded duplicates.
Model-call caps remain separately enforced. `reflection_calls` counts completed
upstream reflection-callable invocations; provider attempts and usage remain in
the actor ledger, including failures/retries.

## Recovery

At upstream iteration-boundary saves, Proteus seals a canonical, hash-bound copy
of the engine state and commits the bridge state and service cursor together.
The data-only tree codec preserves map ordering, integer keys, lists/tuples and
program-index sets. Only the fresh trusted engine's temporary pickle is read;
persisted checkpoints are verified JSON data and are never unpickled directly.
The reference's input pickle is regenerated from verified primitive values in a
fresh private engine directory. An abandoned engine directory is never resumed.

The snapshot includes accepted-ID mappings, seed evidence, proposal decisions,
evaluation counters, sampler frequencies/cursors and shared RNG. On resume, the
upstream engine's unconditional seed bootstrap returns the original checkpoint-bound
seed result without a new evaluation or budget charge. The immediately repeated
state-saved callback does not create an extra journal operation. In-flight work
then replays the same service operations; existing results are reused. Unconfirmed
external calls retain the framework's explicit RecoveryRequired behavior.

The final selected candidate and optional test receipts are checkpointed as a
finished phase before the method result. Repeated resume returns that committed
result without another search, reflection, test evaluation or budget charge.

## Evidence and remaining acceptance

`tests/test_v1_gepa_method.py` compares the integrated loop with the unwrapped pinned
optimizer: sampled tasks, scores, rendered reflection prompts, candidate population,
lineage, acceptance and final output. It covers threshold overshoot, seed-only runs, perfect
score skipping, identical rejected/accepted content and multi-component round-robin
updates. Hard-exit cases cover engine save, sealed snapshot, checkpoint request and
commit, candidate creation, parent selection, reflection, evaluation and final result.
They verify actual fixture invocation files and the persistent ledger after repeated
resume. Modified checkpoints and missing scores fail before extra work.
Adapted archive policies are exercised with different retained populations and
must produce the corresponding parent lineage. A local HTTP fixture also runs the
ordinary ChatActor transport and checks recorded token/model-call usage; it is not
a live-model experiment.

The live-model benchmark, its domain-specific adapter/data split, same-budget
baseline, full result analysis and any additional reference profile remain required
for R01/M2. Those items must not be replaced by these deterministic fixtures.
