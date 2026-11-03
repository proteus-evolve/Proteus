# ACE offline and online recipes (P06b)

[P06c Formula study](V1_ACE_FORMULA.md) supplies the pinned 500/300/200 domain
data, reference metric, multi-replicate controller and complete private report.
Real-model benchmark results remain pending.

This batch implements Generator, Reflector and Curator as separately configured
managed roles, and the complete offline/online control flow from
[`ace-agent/ace@82709de`](https://github.com/ace-agent/ace/tree/82709de050e1db6e6ef2f07bcb0393560b94992a).
The recipe remains **adapted / experimental**: deterministic fixtures and local
HTTP tests establish mechanics, not published benchmark performance. R02 still
requires a domain adapter, a declared real-model experiment and its baseline.

## Contracts and extension points

- `proteus.ace` Method owns ordering, epochs, reflection rounds, validation and
  online windows. Its versioned options reject unused evaluation/stage settings.
- `proteus.ace_subject` consumes a pinned playbook and public `{question, context}`
  task. Its managed actor is configured independently from both update roles.
- `proteus.ace_reflect` and `proteus.ace_curate` are registered stages with typed
  requests/results. Replace either stage with another registered implementation
  of its ports for an explicitly adapted or extended experiment.
- `GenerationInput` carries temporary reflection text and parsing mode. This is
  a **subject input**, not an edit to the playbook, task, or inherited session.
  `open_evaluation(..., inputs={port: DataRef})` and `evaluate(..., inputs=...)`
  resolve registered immutable values before reserving evaluation work. Inputs
  bind protocol/cache identities, dispatch records, predictions and recovery.
- A task grader supplies binary scores and observed training feedback
  `{correct: bool, ground_truth: str}`. The ground-truth string is required only
  when the reflector is allowed to use it. Task references are never placed in
  the Generator request. Domain correctness belongs to the grader; the example's
  string equality is only a synthetic fixture.
- State includes ordered public tasks, current/best candidate IDs, cursor,
  initial tests, validation receipts, online windows and per-sample evidence.
  Checkpoints reuse the common operation journal and ledger.

The codec and role parser/prompt files are opt-in and require no upstream model
SDKs or embedding dependencies. See [playbook contracts](V1_ACE_PLAYBOOK.md) and
the packaged [source notice](../proteus/methods/ace/NOTICE.md).

## Preserved algorithm

For each training sample, generate once with reflection `(empty)`. An initially
incorrect answer receives up to `max_num_rounds` Reflector → counter update →
regeneration iterations; stop those iterations as soon as it is correct. An
initially correct answer still receives one reflection, including when the
configured maximum number of error-correction rounds is zero. Regeneration sees
the full raw reflection. Curator runs at the declared frequency, using the latest
reflection, playbook stats and **task context**, then the method always performs
one post-curation generation with reflection `(empty)`.

Counters, duplicate tags, ignored operations, parser fallbacks, prompt whitespace
and section order follow the pinned source. Stats are recomputed from text at
the Curator boundary to preserve their upstream JSON key order after recovery.
An invalid Curator response is recorded and skipped without a model retry.
The supported operations remain the pinned implementation's counter/ADD behavior;
UPDATE/MERGE/DELETE in a response are retained as ignored operations, not silently
implemented as extensions.

Offline mode preserves declared order and resets `step` each epoch. It evaluates
validation only when `step % eval_steps == 0`; there is no implicit end-of-epoch
validation. Best validation accuracy starts at zero and updates on strict
improvement. Consequently, the selected candidate can remain the initial playbook
when validation never runs or all validation scores are zero. Initial and final
hidden test evaluations use the initial and **best** playbook respectively; the
final current playbook is also returned as `final_candidate`.

Online mode first runs the pinned algorithm's initial whole-stream baseline.
Then each window is scored with one fixed candidate **before any learning in that
window**. Each sample subsequently gets a fresh full training cycle. The next
window receives the updated playbook. Hidden stream scores do not select candidates
or enter role prompts; the observed learning pool explicitly names the same tasks
and grader. Curator frequency uses the global sample step, and only one epoch is
accepted. Online windows are not represented as independent held-out test sets.

## Declared adaptations and limits

1. Managed transport stops on infrastructure failures or invalid/missing training
   scores. It does not reproduce the upstream SDK's retry loop or turn transport
   failures into incorrect answers. Unknown external execution remains subject
   to the common reconciliation rules; recovery does not promise to reissue it.
2. Evaluation is serial. Optional embedding-based BulletpointAnalyzer/deduplication
   is not implemented. `token_budget` is the upstream prompt hint, not an enforced
   truncation or token-count limit.
3. Playbooks must satisfy the immutable codec's unambiguous section/ID contract.
   Imported next-ID state is validated rather than reset to one. Curator edits
   violating that contract are recorded and rejected.
4. The first task contract uses string questions, contexts and training targets,
   and binary correctness. Other data processors need explicit domain adapters.
5. Both modes support a `no_ground_truth` prompt variant; the grader still checks
   correctness, as upstream does. JSON parsing mode must match a configured chat
   actor's JSON response mode. Model choice, temperature, max tokens and optional
   system prompt remain explicit actor configuration and must be reported.
6. Public prediction failures stop the method. Hidden grader validity and complete
   private results must also be audited before any benchmark claim; a successful
   method result alone does not establish successful grading.

## Run the synthetic fixture

From an installed package, create either `--mode offline` or `--mode online`:

```sh
python -m proteus.examples.ace_playbook --out /tmp/ace-study --mode offline
proteus recipe lock /tmp/ace-study/recipe.json \
  --plugin proteus.methods.ace.method:register \
  --plugin proteus.examples.ace_playbook:register \
  --source proteus=/absolute/path/to/Proteus --out /tmp/ace-study/lock.json
proteus recipe run /tmp/ace-study/recipe.json \
  --plugin proteus.methods.ace.method:register \
  --plugin proteus.examples.ace_playbook:register \
  --lock /tmp/ace-study/lock.json --artifact-store /tmp/ace-study/artifact-store \
  --out /tmp/ace-study/run
proteus recipe resume /tmp/ace-study/recipe.json \
  --plugin proteus.methods.ace.method:register \
  --plugin proteus.examples.ace_playbook:register \
  --lock /tmp/ace-study/lock.json --out /tmp/ace-study/run
```

To use a live model, replace each fixture actor with `proteus.chat`, provide the
resolved model and an environment-variable credential reference, and choose the
domain task suite/evaluator. Do not present the synthetic tasks as a baseline.

## Verification

`tests/ace_reference_method.py` checks source hashes, parser executable ASTs and
prompt bytes, then executes the actual upstream `ACE.run`, offline/online loops,
sample algorithm, evaluation functions and three roles. It substitutes only model
transport, token-count telemetry and log sinks. Eight scenarios compare every
role call and full prompt, final text/next ID, best selection and validation score.
They cover initially correct/incorrect answers, zero/multiple rounds, multiple
epochs, Curator frequency, missing periodic validation, JSON/no-GT modes and an
invalid Curator response. This is stronger than comparing two implementations of
the same pseudocode, while still making no model-quality claim.

Additional checks exercise subject input rejection and evidence identity, actual
HTTP payloads/accounting, infrastructure failure without retries, and process
exits at committed prediction/update/checkpoint boundaries and between role
completion and stage publication. Repeated resume must leave role invocation
counts and settled ledger events unchanged. Wheel and sdist checks run both
modes outside the checkout.
