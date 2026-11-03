# Stage evidence views (P07a)

A method can give a research stage a searchable, immutable snapshot of several
candidates, their code, task inputs, rollouts, traces, scores and diagnostics.
There is no mandatory parent sampler or summarization step. This is the evidence
infrastructure for Meta-Harness; it is not yet a Meta-Harness recipe or a claim to
reproduce its benchmark results.

The design was checked against the official Meta-Harness text-classification
implementation at [`8123ccabe2b19fa1123090b2e5f1bacc20963ce8`](https://github.com/stanford-iris-lab/meta-harness/tree/8123ccabe2b19fa1123090b2e5f1bacc20963ce8/reference_examples/text_classification):
the proposer reads historical code, summaries and prediction traces; the outer
loop evaluates its proposed candidates. Test evaluation is a separate finalization
step. Its domain interface, three-candidate proposer workflow and live coding actor
still need their own integration and conformance work.

## Declare access in the recipe

An evidence-enabled `StageSpec` has an explicit `proteus.evidence.view@1` input
and an `evidence` declaration:

```json
{
  "evaluation_ids": ["train", "validation"],
  "splits": ["train", "validation"],
  "kinds": ["candidate", "artifact", "task", "rollout", "trace", "feedback", "evaluation"]
}
```

The declaration is part of the immutable recipe and run configuration hash.
Unknown evaluations/splits and hidden evaluations are rejected when the recipe is
loaded. An omitted declaration grants no access. The role comes from the stage's
existing `role` binding. `kinds` can be narrowed; a code-only view may use empty
evaluation/split lists and `kinds=["candidate", "artifact"]`.

A method creates the view **before** invoking the stage and passes its DataRef as
an explicit input:

```python
history = services.evidence_view("propose")
result = services.stage("propose", {"history": history})
# Optional narrowing: candidate_ids=("candidate-a", "candidate-b").
```

The stage receives a typed `EvidenceView` and can read or search individual files:

```python
view = inputs["history"]
matches = context.search_evidence(view, "retrieval")
code = context.read_evidence(view, path_from_view_entries)
context.materialize_evidence(view, fresh_destination)
```

These APIs check the run, configuration, dependency lock, stage, role and access
declaration, as well as whether the view was passed to this stage invocation.
The method's stage request therefore records the exact disclosed snapshot.
A view for a different stage is rejected before that stage executes.

## Release and provenance rules

- Candidate metadata and exact artifact bytes are retained, including invalid and
  unevaluated candidates. No archive retention policy deletes this history.
- Only explicitly granted observed evaluations appear. Hidden evaluations contribute
  no predictions, grades, task metadata or private record paths to the view.
- `after_prediction` feedback appears after a successful step publication.
  `after_batch` grades appear only after the public batch is committed. Partial
  grading before a failed batch finalization does not release feedback.
- Task inputs are public `Task` records. Private references are not read from the
  dataset. An observed evaluator may deliberately release a reference in feedback;
  that released feedback follows the same timing and access rules.
- Every entry records its candidate, evaluation/split, operation and rollout where
  applicable, source-record hash, byte hash and size. Full feedback and traces are
  retained; no summary substitutes for the original content.
- Evaluation entries may contain scores even when `feedback` files are omitted.
  To grant code alone, exclude **all** evaluation-related kinds. Evidence access
  does not change a batch's eligibility for final candidate selection.

## Storage, export and recovery

`run/evidence/` contains immutable content-addressed blobs and view manifests.
A manifest is published only after all its bytes. Concurrent identical writes are
idempotent; mismatching bytes or a corrupted manifest/blob fail verification.
Snapshot files use digest-derived directories rather than candidate-controlled
path components. Portable artifact paths remain intact inside each candidate.

`EvidenceStore.read` and literal UTF-8 `search` only visit files listed in that view.
Binary bytes are retained and can be read/exported; line search skips files that
cannot be decoded as UTF-8. Exports copy into a fresh destination and publish
`view.json` last. They do not hardlink the store or overlay an older view. An
interrupted export without a valid manifest is incomplete and must be discarded;
use a new destination on retry. `verify_materialized` rejects extra files, changed
bytes and symlinks. File permissions discourage accidental edits, but copies can
be edited without modifying the stored view.

The operation journal records `evidence.view` and the Values snapshot. Recovery
reuses its committed DataRef rather than expanding it with newer history. A crash
after store publication but before journal commit safely repeats the immutable
publication. Old views do not gain subsequently released feedback. Missing or
corrupted evidence is an error, not a reason to silently regenerate it.

Plugins and the host filesystem are trusted. These APIs are disclosure controls,
not an OS sandbox. A future coding-actor backend must mount/copy only the granted
view into its isolated workspace, without the run directory, private evaluator
records or host credentials. Shared notes/skills, mutable handoff state, worker
sessions and concurrent knowledge-update policies are separate planned modules.

## Executable offline example

From a clean source checkout:

```sh
python -m proteus.examples.evidence_history --out /tmp/evidence-study
proteus recipe lock /tmp/evidence-study/recipe.json \
  --plugin proteus.examples.evidence_history:register \
  --source "proteus=$PWD" --out /tmp/evidence-study/lock.json
proteus recipe run /tmp/evidence-study/recipe.json \
  --plugin proteus.examples.evidence_history:register \
  --lock /tmp/evidence-study/lock.json \
  --artifact-store /tmp/evidence-study/artifact-store --out /tmp/evidence-study/run
```

This fixture executes two different Python candidates on validation and hidden
test tasks, then inspects only their validation history. It makes no live model
requests and is not an optimization benchmark. The result contains a `history`
DataRef and an inspection output. To export the recorded view:

```python
from pathlib import Path
from proteus.core.methods import MethodResult
from proteus.evidence.store import EvidenceStore
root = Path("/tmp/evidence-study/run")
result = MethodResult.from_json((root / "result.json").read_text())
EvidenceStore(root / "evidence").materialize(
    result.outputs["history"], "/tmp/evidence-study/history-view")
```

Validation covers real local candidate execution, multi-candidate retrieval,
release timing, hidden-evaluation exclusion, role and invocation binding,
concurrent immutable publication, corrupted content, copy isolation and hard
process exits before/after evidence-journal commit. The wheel/sdist checks run the
example and resume outside the source checkout. Meta-Harness's own method and
real model/domain results remain outstanding.
