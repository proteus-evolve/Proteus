# 1.0 development: methods, stages and actor runtimes

P02 makes the P01 configuration and data contracts executable. A registered `Method`
owns control flow; stages consume and return typed records; actors execute through a
role-specific service boundary. `proteus.native` delegates to the existing episode
engine. Existing `proteus run`, adapter APIs and measurement tools remain available.

Development defaults and schema fingerprints changed since P01. Keep P01 experiment
records with their recorded source version; do not reinterpret a changed hash as a
compatible resume. P02 experiments require a newly resolved lock and manifest.

This is a development API on `dev/1.0.0`. Candidate execution, durable operation
recovery, Harbor, archive/search and the five external recipes follow in later batches.

## Run the offline examples

From a checkout with Proteus installed:

```bash
proteus recipe check recipes/native/minimal.toml
proteus recipe lock recipes/native/minimal.toml \
  --source proteus=. --out /tmp/native.lock.json
proteus recipe run recipes/native/minimal.toml \
  --lock /tmp/native.lock.json --out runs/native-example
proteus recipe resume recipes/native/minimal.toml \
  --lock /tmp/native.lock.json --out runs/native-example
```

The native example runs two offline episodes, with the notes disposition and visible
unit-count feedback. `recipes/examples/stages.toml` runs the generic stage path with
an explicit echo fixture actor and zero model calls. Neither example is a baseline
reproduction or evidence of model improvement.

`recipe list` prints registered plugins, option schemas, capabilities and ports.
`check` resolves option defaults, types, bindings, roles and capabilities without
executing the method. `lock` captures explicit Git source roots, dirty-change hashes,
installed package versions, Python/platform and registered implementation/schema
hashes. Supply one `--source NAME=ROOT` for every plugin source. Place generated
locks outside source trees (or in an ignored run directory). Lock files are created
without replacement. Roots must be Git repositories with an origin; callers working
from other source distributions can construct a `DependencyLock` through the Python
API using their known immutable source references.

`run` verifies the lock against registered implementations and installed versions,
checks role prompt hashes, acquires a run lock and writes an immutable manifest.
It refuses a nonempty run directory. `resume` requires a method declaring recovery;
the native method uses its existing exact durable episode checkpoint. The generic
sequence method currently rejects resume. Regenerate a lock for a changed experiment
and use a new run directory; do not overwrite a completed experiment to change its
conditions.

## Add a self-evaluator stage

An executable example is shipped in
[`proteus/examples/self_evaluator_plugin.py`](../proteus/examples/self_evaluator_plugin.py).
It registers a typed assessment and a stage without changing the engine. Run it with:

```bash
proteus recipe lock recipes/examples/self-evaluation.toml \
  --plugin proteus.examples.self_evaluator_plugin:register \
  --source proteus=. --out /tmp/self-eval.lock.json
proteus recipe run recipes/examples/self-evaluation.toml \
  --plugin proteus.examples.self_evaluator_plugin:register \
  --lock /tmp/self-eval.lock.json --out runs/self-eval-example
```

The stage's result has kind `example.self_assessment@1`. It remains self evidence;
it does not become a formal score or automatically affect candidate selection.
The included deterministic check only demonstrates the interface. A researcher can
replace its implementation with a model-based assessment using `context.actor(...)`.

For a separate package, install it and supply its `package.module:register` hook.
Registration is explicit: configuration files do not automatically import arbitrary
plugin names. Register these pieces:

1. A versioned `Contract` for each data kind, using `registry.register_data`.
2. A `Plugin` with a name/version, kind, option schema, factory, source name,
   implementation hash and declared capabilities. Stage plugins also declare their
   named input/output data kinds. Source hashes must include their implementation
   and relevant helper files; external package dependencies belong in the lock.
3. A stage/config entry naming its role and read/write data kinds. Declared ports
   must agree with the registered signature; omitted signatures are filled in.

Plugin factories and validation hooks must be side-effect-free. Actual model and
tool work belongs in the public execution services. A run uses a frozen registry
snapshot, so plugin registration cannot alter its implementation graph mid-run.
The original registry can be extended for a subsequent experiment.

## Interfaces and control flow

```python
class Method:
    def run(self, services, restored_state):  # -> MethodResult
        ...

class Stage:
    def run(self, context, inputs):          # -> mapping of port names to Contracts
        ...

class ActorRuntime:
    model_call_bound = 1                    # None when no defensible bound exists

    def execute(self, spec, request, workspace):  # -> ActorResult
        ...
```

These interfaces are synchronous in P02. A Method can use Python conditions and
bounded loops, invoke stages multiple times, and return several output references.
`proteus.sequence` provides named input/output slots, an explicit iteration limit,
and a `stop_when` condition checked after each iteration. Equality uses canonical
JSON values, so booleans do not accidentally compare equal to integers. Its first
iteration must have all inputs bound; a reused slot cannot change its data kind.

Each stage submission gets a unique operation ID. Retries get separate attempt IDs
under that operation; successful operations cannot be resubmitted. Input ports and
data references are verified before execution. All output ports are validated before
any output is published. `stop`, `record` and bounded `retry` policies make failure
behavior explicit. `record` returns a failed stage result for a custom method to
handle; the bundled sequence stops when a stage fails.

Stateful methods declare a `state_schema` and `state_version` in their plugin.
Returned and restored `MethodState` must match the run, method, config hash,
namespace and payload schema/version. Unsupported versions are rejected before
calling the method. P01's `StateRegistry` remains available for explicit migrations;
loading never silently migrates stored state. Declaring `run.resume` requires the
method author to implement its recovery semantics. This is sufficient for native
episode recovery or self-contained method state, not a durable generic side-effect
replay system; the latter arrives with the operation ledger.

## Role execution and supported budgets

An `ActorSpec` binds a runtime, resolved model/settings, optional prompt hash,
tools, session policy and budget. Supply role prompt content through `--prompts`
(a JSON mapping from hash to text), or `run_recipe(..., prompts=...)`. Hashes use
`content_hash(text)` from the public serialization module. The framework records
each actor request, its role, prompt hash, operation/attempt, output and actual or
unknown usage. A missing prompt or hash mismatch fails before method execution.

| Runtime | Behavior |
| --- | --- |
| `proteus.echo` | Explicit offline fixture; zero model calls |
| `proteus.chat` | One non-streaming chat-completions request; explicit endpoint/key environment variable, model and settings; records reported model and token usage |
| `proteus.harness.minimal/llm/dsh/pi/codex` | Generic fresh actor operation through an existing adapter, with a separate workspace and a single `act` phase |
| `proteus.native.minimal/llm/dsh/pi/codex` | Native method binding; uses the adapter's original continuity and episode transaction |

The generic harness path supplies no native epistemic/goal protocol. Staged adapters
execute an independent active copy while editing the candidate workspace. Live
harness recipes require an explicit model. Container options are accepted only for
adapters that implement them, and omitted timeout options preserve adapter defaults.
The Codex/DSH/Pi paths require their existing prepared environments and credentials.
P02 tests exercise their bridge contracts with controlled adapter I/O; live release
validation remains required later in the plan.

Generic built-in actors currently support fresh context. Native bindings declare the
actual adapter policy (`fresh` or framework `handoff`); mismatches are rejected.
The public actor interface also distinguishes native session IDs from handoff text
for future/third-party runtimes. The service never substitutes one for the other.

P02 enforces serial model-call reservations across the run, role and stage operation,
including retries. Unknown usage retains the entire reservation. Legacy harnesses
do not have a defensible hard global model-call bound, so finite generic model-call
caps are rejected for them. Native methods retain their explicit episode turn
budgets, whose semantics are different from a model-call count.

Finite cost/time/evaluation reservations and concurrent execution are rejected until
their ledger/scheduler services are present. Built-in chat has no tool loop; existing
harness bridges use their native tool sets and reject unsupported tool allowlists.
Scope declarations enforce typed data-port access and requested tool compatibility;
they are not a filesystem or process sandbox for trusted Python plugins.

## Native compatibility and evidence

Native method options express goal/no-goal, disposition, evaluators and schedules,
selection, phase order/prompts and episode budgets. They are passed to the existing
`RunConfig` engine. Candidate activation/rollback, failed-candidate repair, private
feedback history and episode checkpoints remain owned by that engine. The old CLI
continues to cover legacy adapters and advanced options not yet exposed as recipes.

A read-only prompt observer records each episode's **core-rendered phase prompts**
and native protocol version. Runtime-added messages and live phase-budget headers
remain adapter-specific; this record is not claimed to be every final provider
message. Generic methods never inherit those native prompts implicitly.

Controller files live in the run directory, with the native subject workspace under
`workspace/`. Each completed or handled-failure invocation writes an
`execution-NNNN.json` with operation attempts, actor requests/results, typed values
and native core prompts. Resume writes a new execution file and preserves earlier
ones. `result.json` points to the latest outcome. The native private evaluator
records stay outside its subject workspace.

These execution files are finalized on return or handled failure. They do not yet
provide per-operation crash durability or cross-process budget reservations.
P03a adds the artifact store described below. Formal `services.evaluate` still fails
explicitly until P03b provides candidate task execution and evaluation. Those
requirements remain open in the 1.0 plan.

## Immutable artifacts and candidate operations (P03a)

`ArtifactStore(root, registry)` stores content-addressed blobs, immutable manifests,
candidate lineage and append-only acceptance/rejection/failure decisions. Built-in
codecs are registered like other plugins:

| Codec | In-process value | Deterministic delta |
| --- | --- | --- |
| `proteus.text_components@1` | `TextComponents(components={path: text})` | `TextDelta(base_hash=..., put={...}, delete=(...))` |
| `proteus.structured_records@1` | `StructuredRecords(records={id: fields})` | `RecordsDelta(base_hash=..., put={...}, delete=(...))` |
| `proteus.file_tree@1` | `{path: FileContent(bytes, executable=False)}` | `FileDelta` with base64 `FileWrite`, deletes and simultaneous `Rename` operations |

All deltas name the exact parent content hash. They are data, never executable patch
scripts. Updates return a new `ArtifactRef` and leave parent bytes untouched. Record
updates replace complete records by stable ID; method-specific merges, counters and
curation policies belong in method/stage plugins, not the generic records codec.

```python
from proteus.artifacts.codecs import TextComponents, TextDelta
from proteus.artifacts.store import ArtifactStore
from proteus.core.contracts import PluginRef
from proteus.recipes.builtins import builtin_registry

store = ArtifactStore("artifacts", builtin_registry())
base = store.put(PluginRef(name="proteus.text_components", version="1"),
                 TextComponents(components={"system.txt": "Solve the task."}))
revised = store.apply_delta(base, TextDelta(base_hash=base.content_hash,
    put={"system.txt": "Check the result before answering."}))
store.materialize(revised, "trial-001/candidate")
store.verify_tree(revised, "trial-001/candidate")
```

Manifests hash codec/runtime/environment identity, sorted paths, byte hashes, sizes
and executable flags. Reads verify stored identity and content. Paths reject traversal,
links, special files, conflicting prefixes and case/Unicode aliases. `capture_tree`
captures every regular file without implicit ignore rules; empty directories are not
artifacts. Callers must supply a clean source tree rather than a build/work directory.
`validate` checks byte and codec integrity. Source build/boot validation remains a
separate runtime responsibility and is not implied by a successful store validation.

`materialize` requires a **fresh directory**, copies bytes (never writable hardlinks
to the store), and verifies the resulting tree. It refuses to overlay existing task or
build state. Deletions, renames and absent build caches therefore survive reloading.
Fresh Python subprocess tests exercise two changed code candidates and a later reload
of the original. Container/source-mode runtime integration remains in P04.

Methods use the operation-recording services:

```python
initial = services.initial_candidate()
child = services.fork_candidate(initial, candidate_id="proposal-1", delta=TextDelta(
    base_hash=initial.artifact.content_hash, put={"system.txt": "A proposed update."}))
# services.create_candidate(ref, parents=(...)) also supports multi-parent lineage.
```

`run_recipe(..., artifact_source="artifacts")` or `recipe run --artifact-store artifacts`
copies and verifies the exact initial artifact declared by `SubjectSpec`. The run store
only executes its declared initial codec, which is included in the dependency lock.
Candidate operations record inputs, parent references, delta proposals, attempt results
and zero model-call usage. Failed deltas remain in invocation evidence; a rejected or
invalid candidate keeps its bytes and can be forked for repair. Reusing a candidate ID
for different content fails. `CandidateDecision` records append without rewriting
candidate validation or lineage. Native checkpoint imports require an explicit source
checkpoint and are tagged separately from recipe candidates; native episode snapshots
continue to use the original engine.

Artifact publication is durable and idempotent for identical content. Full method
recovery, replay of completed operations and budget recovery still require P04. This
development API adds executable mode to `ArtifactComponent`; regenerate pre-P03 locks
for a new experiment rather than presenting an old configuration as a compatible resume.
