# Adaptive parallel workflows (P08e)

A workflow plugin runs a bounded program over named stages, formal evaluations
and immutable candidates. A method can map that program over samples: each sample
may need a different number of prediction, reflection and candidate-update steps.
The workflow shares the run's ledger and artifacts, while keeping its service
records, actor instances, evaluation sessions and candidate changes independent.

This is infrastructure for Combee-ACE's per-sample learning loop. It does not yet
deliver the complete Combee-ACE recipe or the dynamic batch controller.

## Plugin and recipe contract

Register a `Plugin(kind="workflow")` with typed `inputs` and `outputs`, versioned
options, an implementation hash, and `workflow.parallel` / `workflow.replay`
capabilities. Its factory returns an object with `run(services, inputs)`, which
returns a mapping of typed output values. A `WorkflowSpec` names the plugin and
the stage/evaluation IDs that this program may use. These declarations and their
schemas enter the normal dependency lock.

The owning method invokes:

```python
mapped = services.map_workflows(
    jobs, operation_id="learn-samples", max_workers=4, failure_policy="collect"
)
```

Each `WorkflowJob` supplies a unique key, a workflow ID and typed `DataRef` inputs.
Results retain input order and publish references to typed values. A failed
program records its diagnostic and publishes no successful outputs. The method
decides how to use the returned `WorkflowMapResult` and whether to stop its own
experiment. A stage configured with `failure_policy="record"` still returns its
failed `StageResult`; the program must inspect that status before proceeding.

Program operations receive stable, map-and-job-specific namespaces. Each branch
starts from the same immutable parent snapshot. New candidate versions and
evaluation records are joined after the programs settle. The parent's random
generator is unchanged; a program receives a deterministic seed derived from the
recipe seed and its namespace. Replaying a program requires deterministic Python
control flow over those inputs and managed operation results.

## Concurrency, budgets and failure

The workflow pool owns concurrency. Evaluations within one program run their
tasks serially, even when the recipe enables parallel standalone evaluations.
Global and per-role slots are reserved in the same SQLite ledger. A busy role
waits for capacity; model-call and evaluation-budget exhaustion remain failures.
Source build cache transactions are serialized while independent trials can run
concurrently. Actor, subject, grader and build entries carry the workflow ID.

`collect` runs all jobs and returns their settled outcomes. `fail_fast` stops
submitting new jobs after a failed program, drains already submitted programs,
and lists unsubmitted keys as cancelled. Recovery never labels an unfinished,
previously submitted program as cancelled. Uncertain external effects stop the
map and retain their reservations.

Workflow stages must declare `stage.parallel` and `stage.replay`; their actor
roles, and a managed subject's role, require fresh parallel-safe actors. Formal
evaluations require independent task state and parallel-safe subject/environment
plugins. The method must enable the durable journal. Workflow checkpoints,
nested maps, native execution and candidate-code calls are currently rejected;
new control-flow mechanisms remain explicit plugins rather than arbitrary
configuration interpreted as executable code.

## Recovery and evidence

The parent journals one map operation. Each branch has a sealed request, its own
operation journal and a final receipt. Recovery validates completed receipts and
ledger bindings before joining them. Submitted unfinished programs first replay
behind a barrier that prevents new external dispatch. A reconcilable subject may
reattach its existing result; a subject without that capability, an unsettled
actor, grader or source build must be resolved before the map can advance.

Programs then continue from managed operation results without repeating settled
calls. Recovery status lists job keys and states without disclosing prompts or
private grades. Interrupted maps report `resumed=true` with no claimed continuous
elapsed duration; a previously committed join retains its original measurement.

The plugin is trusted Python and this API is not a security sandbox. External
work must use managed services. Arbitrary plugin network calls, unrecorded side
effects and mutable global state cannot acquire replay guarantees by declaring a
capability. Harbor/Docker remains the candidate execution boundary when selected.

## Model-free example

The installed example starts three samples from one file-tree candidate. Their
targets require respectively two, three and four formal evaluations. The fixture
uses deterministic echo proposals and trusted local Python; it performs no model
inference and makes no claim about learning quality.

```sh
python -m proteus.examples.adaptive_workflows --out /tmp/workflow-study
proteus recipe lock /tmp/workflow-study/recipe.json \
  --plugin proteus.examples.adaptive_workflows:register \
  --source proteus=/absolute/path/to/Proteus --out /tmp/workflow-study/lock.json
proteus recipe run /tmp/workflow-study/recipe.json \
  --plugin proteus.examples.adaptive_workflows:register \
  --lock /tmp/workflow-study/lock.json \
  --artifact-store /tmp/workflow-study/artifact-store --out /tmp/workflow-study/run
proteus recipe resume /tmp/workflow-study/recipe.json \
  --plugin proteus.examples.adaptive_workflows:register \
  --lock /tmp/workflow-study/lock.json --out /tmp/workflow-study/run
```

The resulting records contain nine predictions, seven candidates and six
model-free stage calls. Finished resume adds no candidate execution or stage
call. `tests/check_workflow_package.py` checks this outside a source checkout in
both distribution formats. Tests also exercise role limits with real local HTTP
requests, multi-role managed subjects, hard exits, receipt integrity and the
reconciliation barrier. Real container coverage runs in the pinned Harbor CI job.
