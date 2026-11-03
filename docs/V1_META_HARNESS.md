# Meta-Harness recipe (P07e)

For the accepted October configurations, actual launch scripts and result boundaries,
see the [frozen experiment guide](../recipes/meta-harness/mce/README.md).
This page documents the general builder rather than a complete frozen run.

This is an adapted, experimental text-classification recipe for
[Meta-Harness at 8123ccabe2b19fa1123090b2e5f1bacc20963ce8](https://github.com/stanford-iris-lab/meta-harness/tree/8123ccabe2b19fa1123090b2e5f1bacc20963ce8).
It includes the full outer search and inner learning loop, two source-pinned
baselines, three bundled MCE domains, validation selection, final test freezing,
private result auditing and durable recovery. Mechanism tests and deterministic
fixtures are separate from real-model experiments. No published score is claimed.

## Method and reusable services

`proteus.meta_harness` is a Method plugin. It uses the shared candidate code
protocol to invoke `inspect`, `predict` and `learn_from_batch`, the shared workspace
stage for a fresh coding proposer, and the shared evaluation, evidence and journal
services. New methods can use those services independently of this recipe.

1. Initialize the configured baselines (by default the upstream `no_memory` and
   `fewshot_all`). Evaluate each dataset/seed separately, with fresh initial memory.
2. Offline mode discloses only explicitly authorized training labels and learns
   batches without inventing training predictions. Validate after each epoch,
   keep the first strictly best epoch, and restore its serialized memory when
   necessary. Online mode predicts all examples in a batch before learning from
   its feedback; it uses one pass.
3. Save the learned memory before the final validation pass, as the upstream
   controller does. Record validation accuracy and average injected context
   characters, measured from the last actual prompt minus the input length.
4. Give the proposer immutable source, summaries, learning records and a scoped
   file view of all released prediction traces. No parent selector is imposed.
   Capture a multi-file output artifact containing `agents/`, `pending_eval.json`
   and optional reports. Import-check each proposed memory system, then run the
   same learning/validation protocol for it.
5. Aggregate complete seed groups, find the accuracy/context Pareto frontier and
   the best system per dataset. Freeze the sorted union of baselines, frontier
   systems and dataset winners before dispatching any hidden tests.
6. Test each selected system on every declared dataset/seed using its saved
   pre-final-validation memory. No learning or proposing occurs after freezing.

`EvaluationSpec.allow_supervision` defaults to false. Setting it requires an
observed optimization pool, and overlapping validation/hidden task identities
are rejected. `services.supervised_batch(id)` returns references aligned with the
pinned public task order, records the disclosure, and charges no evaluation or
model call. This API does not grant filesystem permissions to a coding actor;
workspace input permissions remain separate.

## Reference scope and adaptations

The package preserves the upstream memory classes, evaluators and proposer text,
including the MIT license. The provider-bearing `llm` module is reduced to its
actual `LLMCallable` protocol. MCE loader function bodies are unchanged; unused
transfer-data helpers and the eager Hugging Face import are omitted.

The supported complete data splits are:

| Dataset | Train | Validation | Test |
| --- | ---: | ---: | ---: |
| USPTO | 50 | 30 | 100 |
| Symptom2Disease | 200 | 50 | 212 |
| LawBench | 200 | 50 | 100 |

Preparation verifies all selected source JSONL bytes against pinned hashes, uses
the original prompt/context formatting, and shuffles each split independently
with the declared seed. The normalized datasets also have fixed content hashes.

The following differences are recorded in the recipe:

- Execution is serial. Each candidate call runs in a fresh process; only
  `get_state`/`set_state` memory persists. Nonserialized globals, thread-local
  state and RNG state are not implicitly preserved. RNG is initialized per call.
- Model calls use managed ActorRuntime calls. Proposer runtime/model are supplied
  explicitly; using DSH, Pi or Codex does not reproduce the original Claude Code
  runtime, cost or latency.
- The original proposer instructions are accompanied by explicit directory and
  runtime adaptations: read-only input/history, writable scratch/output, managed
  models and the configured candidate image. Its original `uv` commands and
  provider SDK environment are not implicitly available.
- Proposal names must be new and unique; typed metadata is required. A malformed
  manifest rejects that proposal batch. A valid manifest can contain a variable
  number of candidates, matching the outer controller, although the prompt asks
  for three. Candidate import failures are recorded individually.
- Selection requires all declared dataset/seed groups to be complete. The
  upstream code can aggregate partial dataset coverage. Exact ties use stable
  system-name order; identical Pareto points remain distinct candidates.
- Known results replay from durable records. An externally dispatched operation
  with no settled receipt remains uncertain and stops recovery; it is not retried
  with an invented result or reset budget.

This profile does not claim arbitrary process-state equivalence, all upstream
domains, or the original parallel benchmark schedule. Custom classes must follow
the same serialization and model-callback contract. The initial file library and
every captured proposal are immutable artifacts; declared `base_system` is kept
as research metadata, without inventing formal parent lineage.

## Run the deterministic integration fixture

The host-local fixture runs trusted bundled Python and performs no inference.
Run these commands from a clean source checkout, or set `--source` to one:

```sh
python -m proteus.examples.meta_harness_fixture --out /tmp/meta-fixture
proteus recipe lock /tmp/meta-fixture/recipe.json \
  --plugin proteus.methods.meta_harness.method:register \
  --plugin proteus.examples.meta_harness_fixture:register \
  --source proteus=. --out /tmp/meta-fixture/lock.json
proteus recipe run /tmp/meta-fixture/recipe.json \
  --plugin proteus.methods.meta_harness.method:register \
  --plugin proteus.examples.meta_harness_fixture:register \
  --lock /tmp/meta-fixture/lock.json --artifact-store /tmp/meta-fixture/artifact-store \
  --out /tmp/meta-fixture/run
python -m proteus.methods.meta_harness.report --run /tmp/meta-fixture/run \
  --plugin proteus.examples.meta_harness_fixture:register
proteus recipe resume /tmp/meta-fixture/recipe.json \
  --plugin proteus.methods.meta_harness.method:register \
  --plugin proteus.examples.meta_harness_fixture:register \
  --lock /tmp/meta-fixture/lock.json --out /tmp/meta-fixture/run
```

The fixture reads multiple baselines, submits a multi-file learning system, a
wrong-answer system and an invalid import, then selects and tests eligible
systems. The report labels its data and actors as fixtures. CI also runs this
complete method with candidate code inside the pinned Harbor/Docker backend;
the proposer/model there remain deterministic fixtures.

## Prepare a real experiment

Use the isolated pinned Harbor runtime described in [V1_HARBOR_CODE.md](V1_HARBOR_CODE.md).
Prepare two ActorSpec JSON files: one for the prediction model and one for a
workspace-capable coding proposer. The latter can use
`proteus.workspace.dsh`, `proteus.workspace.pi` or `proteus.workspace.codex`; see
[V1_CODING_WORKSPACES.md](V1_CODING_WORKSPACES.md) for runtime and image settings.
Credentials stay in the runtime's documented environment variables.

```sh
git clone https://github.com/stanford-iris-lab/meta-harness.git /tmp/meta-reference
git -C /tmp/meta-reference checkout 8123ccabe2b19fa1123090b2e5f1bacc20963ce8
python -m proteus.examples.meta_harness --out /tmp/meta-study \
  --reference-source /tmp/meta-reference \
  --model-config model.json --proposer-config proposer.json \
  --harbor-python /path/to/pinned-harbor/bin/python \
  --datasets USPTO Symptom2Disease LawBench --seeds 42 \
  --mode offline --num-epochs 1 --batch-size 1 --iterations 1
```

Preparation makes no inference calls. Inspect the generated recipe and set the
experiment budgets before locking it. Opaque coding runtimes cannot satisfy a
finite global model-call cap; their unknown usage stays unknown. Use explicit
runtime timeouts, candidate call limits and evaluation budgets that the selected
backend can enforce. Lock/run/resume as above, omitting the fixture plugin and
using the real study paths. Run the private report after execution.

The read-only report verifies source/schema locks, datasets, learning requests
against their settled receipts, validation scores, sequence state continuity,
saved-best memory, selected cohorts and the freeze boundary. It regrades hidden
predictions and rejects missing/unavailable scores, changed grades, unreported
cohorts or mismatched learning evidence. It currently refuses a publication
report when a candidate has an incomplete learning/validation group; the run's
raw diagnostics and receipts remain available. Constructor failures have no
benchmark group and remain visible in iteration records.

Real-model experiments, their budgets/provider configuration, and published-score
comparison remain release acceptance work. A completed fixture or green CI does
not change `reference.status` to `benchmark_validated`.
