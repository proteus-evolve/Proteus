# 1.0 development: experiment contracts

P01 introduces the data and configuration API used by the implementation plan. It
targets `dev/1.0.0`; the public package version stays 0.4.0 until the release gates
are complete. These interfaces may change during development. The existing episode
API and `proteus run` retain their current behavior.

## Inspect a configuration

From the checkout:

```python
from proteus.recipes import load_recipe

recipe = load_recipe("recipes/examples/p01-contract.toml")
print(recipe.config_hash)
print(recipe.to_json())
print(recipe.json_schema())
```

The example is a **configuration inspection fixture**, with zero model/evaluation
budget and deliberately unregistered `example.*` plugin names. It validates the
configuration API; it does not run an optimization method. P02 supplies the method,
stage and runtime registry. P03/P04 supply candidate execution and persistence.

P02 is now implemented: see [V1_PLUGINS.md](V1_PLUGINS.md) for runnable native and
stage examples, plugin validation and current execution limits. Task/subject/environment
declarations may be omitted when a method does not use those services, including
native no-goal experiments; each method validates the components it requires.

JSON and TOML are accepted. Unknown framework fields, duplicate JSON keys,
nonfinite numbers, unsupported schema versions and incorrect primitive types are
rejected. No model calls or plugin imports occur while loading. Strings are not
expanded from environment variables; provide resolved model identifiers. Plugin
`options` are immutable JSON, with plugin-specific validation delegated to the
registry introduced in P02.

Overrides recursively merge objects and replace arrays or scalars. Explicit zero
is a zero budget; `None` (JSON `null`, or an omitted TOML field) is unlimited.

```python
recipe = load_recipe("recipes/examples/p01-contract.toml", overrides={
    "budget": {"max_model_calls": 20},
    "actors": {"optimizer": {"model": "resolved-model-version"}},
})
```

Defaults are included in `to_json()`, so the exported result is a fully expanded
configuration. Actors, subjects, task suites, evaluation protocols, environments,
stages, search plugins and budgets occupy separate fields. A subject/stage actor
must exist. A state policy must agree with its environment lifecycle. Test
evaluations are hidden and ineligible for selection. Capability checking accepts
the registry's declarations; it does not probe or certify external infrastructure.

## Record boundaries

| Record | Meaning and enforced invariants |
| --- | --- |
| `ArtifactRef` | Digest, codec/version, component paths/hashes, optional runtime/environment identity; unique, portable component paths |
| `Candidate` | Run, parent IDs, immutable artifact reference and creating operation/attempt; validation status is separate from selection |
| `Rollout` | Actual candidate/artifact, task/version/split, runtime/settings, execution hash, output/trace, usage and state before/after; sequence ID and step occur together |
| `EvaluationBatch` | Candidate, evaluator/version, protocol hash and an explicitly ordered cohort of rollout results; no caller-supplied aggregate score |
| `StageRequest` / `StageResult` | Operation/attempt identity, role, references, scopes, budget, outcome and usage |
| `MethodState` | Plugin payload, config hash, archive reference, RNG state, cursors and pending operations |
| `RunManifest` | Resolved recipe, exact source/dependency/plugin lock and verified config/lock hashes |

Every contract supports `to_dict()`, `to_json()`, `from_dict()`, `from_json()` and
`json_schema()`. Records are frozen, including nested mappings and arrays. Exported
dicts are detached copies. JSON Schema draft 2020-12 covers structural constraints;
constructors additionally check semantic relationships. The runtime also rejects
type coercions that JSON Schema itself permits (for example `1.0` as an integer).

`schema_version=1` is the common record format. A plugin's `payload_version` is a
separate version axis. `StateRegistry` requires an explicitly registered payload
schema and validates it when restoring. Migrations are registered one version at a
time and run only through an explicit `migrate` call; old records are not modified.

### Failure and score semantics

`succeeded` and `task_failed` evaluation rows require a finite score. A genuine
failed task may score zero. `invalid_candidate`, `timed_out`, `infra_error`,
`score_missing` and `cancelled` have null scores and unavailable scored metrics.
Whether a deadline counts as a scored task failure or an execution timeout must
be decided explicitly by the evaluator adapter's declared protocol.

`EvaluationBatch.mean_score()` returns the mean only for a complete scored cohort.
If any row is unavailable, it returns `None`; it never shrinks the denominator.
`Usage` defaults to unknown values, including when calls fail before reporting
usage. Missing usage is not free execution.

`validate_records(...)` checks a complete exported record set: run identity,
unique IDs, candidate parents and acyclic lineage, operation/attempt references,
retry consistency, candidate artifact identity, evaluation-to-rollout task/candidate
alignment, and duplicate operation completion. Include the seed/import request
when validating initial candidates. This function validates references; checking
artifact bytes, actual execution identity, evidence permissions and protocol timing
belongs to the execution/store services in subsequent batches.

## Freeze an experiment

Construct a `DependencyLock` from the resolved environment. It records exact Python
and package versions, immutable repository commits (including a `proteus` source),
optional dirty-patch/distribution hashes and each plugin's implementation hash,
source and capabilities. Every plugin referenced by the recipe must be locked,
including artifact codecs and artifact-specific runtimes.

```python
from proteus.recipes import RunManifest

# resolved_lock is a DependencyLock populated from the experiment environment.
manifest = RunManifest.create(run_id="experiment-001", recipe=recipe, lock=resolved_lock)
manifest.write("runs/experiment-001/manifest.json")
manifest.assert_resume_compatible(recipe, resolved_lock)
```

The manifest checks recipe/lock hash consistency both on construction and reload.
Hashes use UTF-8 JSON with sorted keys, no whitespace, no nonfinite values and a
`sha256:` prefix. This is Proteus canonicalization, not a claim of RFC 8785 support.
Hashes establish content consistency; they are not signatures or proof that a
declared runtime/source was executed. The lock describes versions supplied by the
resolver; source collection and runtime verification are later service responsibilities.

`write` publishes the complete file atomically without replacing an existing one.
Writing the identical manifest is idempotent; writing different bytes at that path
fails. A resume check rejects changes to either the resolved recipe or dependency
lock. This is the identity check for future recovery services, not an implemented
checkpoint/resume engine.

## Validation and remaining work

`tests/test_v1_contracts.py` checks serialized round trips against an independent
JSON Schema validator, invalid primitive/version inputs, immutable nested state,
failure semantics, cohort alignment, long/cyclic lineage, retry identity, explicit
state migration, configuration overrides, capabilities, manifest integrity and
no-replace publication. The validator is a development-only dependency; production
contracts use the standard library.

P02 integrates registered plugin option schemas and capability declarations with
execution and exposes the native compatibility recipe. P03/P04 enforce artifact
identity and evaluation protocols against real task execution, introduce the event
ledger and implement recovery. P05–P09 implement the five external method families.
P10 completes researcher entry points and release validation. A valid configuration
or contract test does not count as a reproduced method or a real benchmark result.
