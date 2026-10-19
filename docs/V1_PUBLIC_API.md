# Versioned researcher API

New method, stage, evaluator, subject, actor and search-policy plugins can import
from `proteus.api.v1`. This is the proposed public import surface for 1.0; the
project remains a development build until release acceptance is complete.

```python
from proteus.api.v1 import (
    Contract, EmptyOptions, Plugin, PluginRef, PolicyResult, SelectionView,
    implementation_hash,
)
```

These names alias the actual execution contracts. They are not parallel schemas
or wrappers that reinterpret an old experiment. `API_VERSION` is `1`; use the
explicit versioned module instead of depending on incidental internal imports.

| Area | Public interfaces |
| --- | --- |
| Contracts | `Contract`, `ContractError`, canonical JSON/hash helpers; candidate, artifact, rollout, evaluation, usage and state records |
| Registration | `Plugin`, `Registry`, `EmptyOptions`, `PluginRef`, `implementation_hash`, `builtin_registry` |
| Methods and stages | `Method`, `MethodResult`, `RunServices`, `Stage`, `StageContext`; typed actor/stage requests and results |
| Actors and evaluation | `ActorRuntime`, `ActorRequest`, `ActorResult`, `SubjectRunner`, `SubjectContext`, `SubjectResult`, `Evaluator`, `Grade` |
| Search | `ParentSelector`, `ArchivePolicy`, `FinalSelector`, `SelectionView`, `PolicyResult`, `SearchDecision`, `ArchiveSnapshot` |
| Artifacts and tasks | File/text/record values and deltas, `ArtifactStore`, task datasets/batches/selections and `TaskSuite` |
| Offline measurement | `MeasurementSpec`, `MeasurementSurface`; see [supported artifact units](V1_ARTIFACT_MEASUREMENTS.md) |
| Experiment setup | Recipe/role/stage/subject/environment/evaluation specs, source/dependency locks, `RunManifest`, `load_recipe`, `run_recipe` |

The exact symbol list is `proteus.api.v1.__all__`. Method-specific optimizer
internals, example algorithms, SQL tables, recovery journal files, worker transport
internals and private attributes are not added to the stable API by this module.
Advanced backends continue to have explicit source-pinned contracts and capability
checks; importing `RunServices` is not a stability promise for every internal field.

## Service contracts used by extensions

A method receives framework-owned services. The supported extension surface is:

- Read the resolved `recipe`, run identity, candidate/rollout/evaluation history,
  and use `values.publish` / `values.get` for declared typed values.
- Create/fork exact candidates through `initial_candidate`, `create_candidate`
  and `fork_candidate`; use the artifact store's checked value/delta/materialize
  operations. Do not mutate the stored history lists or immutable artifacts.
- Use `task_pool`, `open_evaluation` / `evaluate`, `archive_snapshot`,
  `selection_view`, `select_parents`, `retain_candidates` and `select_final`.
- Invoke configured stages through `stage`, `map_stages` or `map_workflows` and
  checkpoint the method's typed private state through `checkpoint`.
- A stage accesses its actor through `StageContext.actor`, and only its declared
  evidence/knowledge views through the context's read/search/materialize methods.
- A subject receives the exact candidate lease, public task/state/history/inputs,
  runtime settings and seed through `SubjectContext`; managed model work uses
  `context.actor`. Private task references are supplied to the evaluator only.

Framework services own budgets, evidence publication and recovery. Plugins must
not bypass them with unrecorded external work. Search policies are pure decisions:
use the supplied RNG, return in-view IDs and reasons, and declare the relevant
`search.parent`, `search.archive` or `search.final` capability. Trusted Python
extensions are not a process security boundary.

## Extend a parent selector without editing core

```sh
proteus recipe scaffold policy --name my_parent --out /tmp/my-parent
cd /tmp/my-parent
python prepare.py
python check.py
```

The generated plugin implements `select_parent(view, rng)` using only the public
API for registration and policy contracts. The demo method/evaluator are separate
bundled examples. The editable rule chooses the candidate with the highest minimum
validation score. The check switches only `search.parent` to `proteus.scalar_best`:

| Condition | Selected parent | Same child update | Child final score |
| --- | --- | --- | ---: |
| External worst-task selector | balanced | add 0.05 to predictions, capped at 1 | 0.45 |
| Scalar-best selector | spiky | add 0.05 to predictions, capped at 1 | 0.85 |

Both conditions see the same three parents and validation vectors. Each performs
nine formal evaluations: six parent validation trials, two child validation trials
and one private final trial. The child stores its selected parent and changed
artifact; predictions are read from the actual materialized candidate. The check
also resumes without repeated work and exports public/private reports outside the
repository. This deterministic example demonstrates the extension mechanism, not
a claim about algorithm quality.

The existing four templates also import plugin contracts from `proteus.api.v1`.
See [V1_PLUGIN_SCAFFOLDS.md](V1_PLUGIN_SCAFFOLDS.md) for the self-evaluator example,
and [V1_COMPATIBILITY.md](V1_COMPATIBILITY.md) for locks and stored-run compatibility.
