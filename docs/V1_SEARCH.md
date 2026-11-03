# 1.0 development: task selection, archives and search policies

P05a supplies shared search primitives. P05b adds [pinned GEPA search and task
sampling](V1_GEPA_SEARCH.md); P05c adds the [executable GEPA method](V1_GEPA_METHOD.md)
for the declared no-merge profile. These do not claim benchmark/paper reproduction.

## Select tasks from a declared pool

```python
from proteus.evaluation.tasks import TaskSelection

pool = services.task_pool("train")  # TaskBatch: public inputs/state, no references
selection = TaskSelection(task_ids=("task-2", "task-0", "task-2"))
receipt = services.evaluate(candidate, "train", selection=selection)
# Online methods can instead use:
session = services.open_evaluation("train", selection=selection)
```

The recipe's `EvaluationSpec.tasks.task_ids` remains a unique, pinned pool in one
dataset split. Inspecting it reserves no evaluation. An explicit selection preserves
exactly its order, including duplicates; the pool's shuffle is not reapplied.
Omitting selection retains the full-cohort behavior. Every occurrence has its
original task ID and a separate rollout ordinal, seed and budget charge. Three
occurrences cost three evaluations. The entire selected cohort is reserved
atomically; unknown/out-of-pool IDs fail before reservation.

The selection enters the public task batch, protocol identity and durable journal.
Resume restores the original occurrences and service state. Changed selections are
changed operations, not continuations. `task_pool` is also journaled and never
returns reference answers. Hidden feedback, sequence state and feedback timing
retain their existing semantics.

## Keep history separate from the search population

```python
archive_ref = services.archive_snapshot()
archive = services.values.get(archive_ref)
candidate = archive.candidate("candidate-000002")
children = archive.children("initial")
evidence = archive.evidence(candidate.id, "validation")
```

The immutable `ArchiveSnapshot` (`proteus.archive@1`) includes all created
candidates/parent links, publicly released evaluation batches with their task
cohorts, and prior search-decision references. Failed evaluations and rejected
candidates remain queryable. New snapshots never modify old ones. Pass the reference
to `services.checkpoint(payload, archive=archive_ref, ...)` to name it in MethodState.

Candidate creation is independent of automatic selection. An agent can call
`create_candidate(artifact, parents=(a, b))` directly. Declaring search plugins does
not force it to use them; policies neither create candidates nor delete history.

Only observed evaluations enter the archive. Hidden test grades remain private.
Observed but selection-ineligible evaluations are queryable but cannot produce a
search view. These are API contracts for trusted plugins, not a Python/file sandbox.

## Choose comparable evidence explicitly

```python
view = services.selection_view(archive_ref, "validation")
parents = services.select_parents("parent", view)
active = services.retain_candidates("population", view)
final = services.select_final("final", view)
chosen_ids = final.result.selected
```

An optional `candidate_ids=(...)` argument to `selection_view` defines an ordered
population of existing candidate IDs. It filters membership and preserves that
order, while all original archive evidence stays intact. Ineligible members remain
excluded; no scores are fabricated. The population is part of the view/journal
identity. Methods such as GEPA require the explicit accepted-program order so
rejected proposals and equal-content candidates cannot change program indices.

These names resolve to independently configured plugins in `RecipeConfig.search`.
A view requires a declared, observed, selection-eligible non-test evaluation and
checks the snapshot against committed history. By default each task occurrence is
one score dimension. Candidates must complete the same full cohort and protocol;
partial online candidate cohorts are excluded. When an evaluation has multiple
cohorts/protocols, pass its intended `protocol_hash` explicitly. Scores from
different minibatches are not silently compared.

For each candidate the most recently completed batch under that protocol is used.
A failed recent batch is excluded, not replaced by an older favorable score.
Invalid, unevaluated and incompletely scored candidates receive exclusion reasons.
Missing scores are never zero-filled or dropped from the denominator. Historical
views remain usable after new evaluations arrive; policy calls revalidate their
source evidence instead of trusting caller-invented scores.

Aggregate metrics are explicit:

```python
from proteus.core.search import Objective

view = services.selection_view(archive_ref, "validation", objectives=(
    Objective(metric="score"), Objective(metric="cost", maximize=False),
))
frontier = services.select_final("frontier", view)
```

Every named metric must appear on every row. Its cohort mean is used; minimization
values are negated so vectors consistently mean “larger is better.” These are
evaluator-provided metrics, not an assertion that opaque runtime costs are known.

## Policies and audit records

| Plugin | Roles | Rule |
| --- | --- | --- |
| `proteus.scalar_best@1` | parent, final | Maximum mean task score, or one aggregate objective; earliest candidate breaks ties |
| `proteus.uniform_parent@1` | parent | One uniform draw using the framework RNG |
| `proteus.keep_all@1` | archive | Retain every eligible scored candidate |
| `proteus.pareto_front@1` | archive, final | All non-dominated vectors, including exact ties |

Generic Pareto retains local advantages that a mean can hide; it is **not** GEPA's
instance-frontier pruning/sampling. Scalar selection rejects multiple aggregate
objectives instead of assigning implicit weights to different metric units.

Custom plugins use kind `policy`, with `search.parent`, `search.archive` and/or
`search.final` capabilities. Interfaces in `proteus.core.search` are:

- `ParentSelector.select_parent(view, rng) -> PolicyResult`
- `ArchivePolicy.retain(view, rng) -> PolicyResult`
- `FinalSelector.select_final(view, rng) -> PolicyResult`

Policies are pure decisions: use the supplied RNG and return selected IDs plus a
reason for every eligible candidate. They receive no actor/execution service;
external work belongs in method/stage services. Out-of-view candidates, omitted
reasons and unsupported roles fail explicitly. Parent/final results must be
nonempty; archive retention may return an empty set.

Each call creates public StageRequest/StageResult records and a typed SearchDecision
with policy/options, source view, selected IDs, all reasons and before/after RNG
states. Journal replay restores it without another draw. Failed calls remain failed
operation records. A plugin may return multiple parents or a final frontier.

## Validation and compatibility

Tests execute local subjects for repeated samples, budget limits, sequence state,
privacy and partial cohorts. Score matrices cover scalar, generic Pareto, ties and
minimization. Subprocesses hard-exit after pool/batch/rollout/archive/view/selection/
checkpoint commits, then check invocation counts, decisions and unchanged accounting.

This development change adds TaskBatch selection and operation identities. New runs
need regenerated locks. Existing runs require their original source/lock; journals
are not migrated or rewritten. The full method/benchmark acceptance and remaining
P04 work remain open in the implementation plan.
