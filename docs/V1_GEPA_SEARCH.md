# 1.0 development: GEPA reference search and task sampling

P05b integrates GEPA's instance-frontier parent selection and epoch-shuffled
minibatches as reusable building blocks. It does **not** register a complete GEPA
Method or claim a benchmark reproduction. Reflection, acceptance, component
mutation, optional merge, engine checkpoints and end-to-end method conformance
remain P05 work. The search profile requires complete, comparable validation
vectors for every accepted program; objective/hybrid frontiers and sparse
validation are not covered.

## Exact upstream identity

The optional runtime is [gepa-ai/gepa at
8b0ce6c](https://github.com/gepa-ai/gepa/tree/8b0ce6cd99a234f6b74daf37558a2ac0ce18f975),
the `v0.1.4` tag. Its distribution metadata declares `0.1.3`, so package version
alone is insufficient. Install that source into the Proteus Python environment:

```sh
python -m pip install 'gepa @ git+https://github.com/gepa-ai/gepa.git@8b0ce6cd99a234f6b74daf37558a2ac0ce18f975'
```

Before importing it, the optional plugin verifies all 91 upstream Python source
files against `sha256:ccf1e6140d7ebd2190e9ee0b1b9558b2ab9a8673366d61b748f7c43d72fcad50`.
The digest is Proteus canonical JSON of the relative-path → file-SHA256 mapping,
hashed again with SHA256. The plugin implementation lock binds that identity and
the Proteus bridge source. A different/missing installation fails before execution;
there is no approximate fallback. No GEPA dependency or import is added to the
default registry. This is a controlled Python runtime, not protection against
hostile in-process monkeypatching or filesystem mutation.

Explicitly load the search plugins with
`--plugin proteus.methods.gepa.search:register` for `recipe check`, `lock`, `run`
and `resume`, or call `register(registry)` from a trusted method host. The usual
recipe lock also records installed distributions. The exact upstream commit and
source digest above identify the reference, independently of distribution metadata.

## Accepted population versus archive

```python
from proteus.methods.gepa.search import register

register(registry)  # before resolving/freezing the run registry
# RecipeConfig.search declares these independent policy slots:
# parent:    proteus.gepa_parent@1
# population: proteus.gepa_frontier@1
# final:     proteus.scalar_best@1 (a separate final-selection choice)

view = services.selection_view(
    services.archive_snapshot(), "validation",
    candidate_ids=tuple(accepted_program_ids),
)
frontier = services.retain_candidates("population", view)
parent = services.select_parents("parent", view)
```

`candidate_ids` is an ordered tuple of distinct existing candidate IDs. Its order
defines upstream integer program indices. Equal artifact content never merges
identities. Other archive entries remain recorded and receive an explicit
`outside declared population` exclusion. Hidden/ineligible/test evidence cannot
enter a view; invalid or incompletely scored population members still have their
normal exclusions. The GEPA policy rejects those exclusions rather than silently
renumbering the accepted program pool. It also rejects aggregate objective views.

The full ordered population is part of the view hash and durable journal identity.
Changing it during replay is an input mismatch. General-purpose policies may use
the same population facility; omitting it retains the whole-archive behavior.
It defines membership and ordering, not a GEPA acceptance rule: the method must
maintain its accepted IDs according to the upstream optimization algorithm.
The view chooses the evaluation cohort/protocol. Binding that cohort to the
method's intended full validation dataset is also the method adapter's job;
these policies do not infer a dataset from scores or validate a complete recipe.

For each validation task, the bridge constructs the integer-index set of tied
maximum-scoring programs. It then calls the unmodified upstream
`remove_dominated_programs` and `select_program_candidate_from_pareto_front`.
GEPA prunes redundant coverage witnesses in increasing aggregate-score order,
preserving the reference's tie behavior, and samples according to the number of
fronts each surviving program covers. It is **not** generic coordinate dominance:

| Validation scores | Generic Pareto | GEPA coverage set |
| --- | --- | --- |
| A: `[1, 0]` | retained | retained |
| B: `[0, 1]` | retained | retained |
| C: `[0.6, 0.6]` | retained | excluded: no per-task maximum |

If a later accepted D ties A exactly, the reference can remove A as redundant;
generic Pareto keeps both. The final scalar-best candidate can still be C. A
coverage set and the final selected solution are separate concepts. The GEPA
coverage plugin intentionally has no `search.final` capability.

Search decisions record the pinned plugin, immutable view, candidate reasons,
instance wins/parent weight and RNG before/after. They invoke no model and reserve
no evaluations. Archive retention does not delete candidates or feedback.

## Task sampling and method checkpoints

```python
from proteus.methods.gepa.sampling import EpochSampler, SamplerState

pool = services.task_pool("train")
sampler = EpochSampler(pool, minibatch_size=3, rng=services.rng)
selection = sampler.select(iteration=0)
receipt = services.evaluate(candidate, "train", selection=selection)
saved = sampler.snapshot()
# Include `saved` in the calling method's registered MethodState payload.
# After restoring the same pool, minibatch size and journal cursor:
sampler.restore(SamplerState.from_json(saved.to_json()))
```

The wrapper calls the upstream `EpochShuffledBatchSampler` with the original task
IDs and the same RNG as parent selection. It preserves epoch shuffle, repeated
calls within an iteration, and least-frequent-ID padding when a pool size is not
a multiple of the minibatch size. Duplicate task occurrences remain independently
executed and charged by Proteus.

Checkpoint state includes the complete pool hash, batch size, shuffled occurrence
list, ordered frequency counter, epoch, iteration/call cursor and RNG state. Counter
order is an ordered record sequence because sorted JSON object keys would change
padding ties. Restore validates state before mutating the shared generator; changed
pool identity, batch size and inconsistent occurrence/frequency state fail.

Sampling is a method building block, not a separate journal operation. The method
must checkpoint it with its control cursor and the framework service state. A
restart from a seed alone is insufficient. The pool is immutable for a wrapper's
lifetime; changing the training dataset requires a new sampler/protocol. Calling
iterations backwards is rejected.

## Verification and remaining work

`tests/test_v1_gepa_search.py` runs against the installed pinned source. CI installs
it and requires those tests on Python 3.10–3.14; local runs may skip them if the
optional runtime is absent. Set `PROTEUS_REQUIRE_GEPA=1` to make absence a failure.

Conformance tests construct actual upstream GEPAState objects, including identical
program content, and compare front mappings, aggregate scores, retained indices,
seeded parent sequences and resulting RNG states. They cover ties, negative
scores, 1–70 programs, padding, repeated iteration calls and JSON state restoration.
Service tests use real local subject processes to verify archive membership,
visibility constraints and no evaluation charges for selection. Hard-exit tests
resume search and sampler checkpoints without additional subject invocations;
changed population replay is rejected.

These fixtures establish the documented search/sampling behavior. They do not
establish full GEPA engine recovery, reference-paper results, model effectiveness
or a five-method recipe release. Remaining P05 work must map accepted upstream
program indices to candidate lineage, route actual reflection through ActorRuntime,
handle the upstream seed-evaluation-on-resume behavior, seal engine state, and
validate complete recipes and budget boundaries before benchmark claims.
