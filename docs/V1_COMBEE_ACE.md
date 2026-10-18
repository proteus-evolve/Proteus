# Fixed-batch Combee-ACE recipe (P08f)

`proteus.combee_ace` composes the existing ACE subject/reflector/playbook, adaptive
[workflow maps](V1_WORKFLOWS.md), [Combee aggregation](V1_COMBEE_AGGREGATION.md)
and formal evaluation services. Its reference is `ACEBatch` in
[`ace-agent/ace@82709de`](https://github.com/ace-agent/ace/tree/82709de050e1db6e6ef2f07bcb0393560b94992a).
The supported profile is adapted/experimental. The fixed-batch method is distinct
from the future dynamic batch controller and from real-model benchmark validation.

## Three phases

1. Every sample starts from the same immutable batch playbook. Its workflow
   generates once. An incorrect sample independently reflects, updates its local
   counters and regenerates until correct or its round limit is reached. An
   initially correct sample reflects once, including when the round limit is
   zero, and does not regenerate or edit local counters before shared curation.
2. The owner combines original sample tags once. The reference counter updater
   retains the last tag for each repeated bullet ID within that update; tags are
   not independent increments. Augmented copies enter only reflection grouping.
   Every curator group reads the same counter-updated base. Only reducer ADD
   operations modify the shared playbook and allocate global IDs.
3. The batch generates again from that shared playbook with an empty temporary
   reflection. Known prediction/grade failures restore the immutable pre-batch
   playbook, including its next global ID, and stop the method. Attempted changes
   and actual failed calls remain in the evidence. The upstream API-error sentinel
   is not converted into an ordinary incorrect score. Uncertain dispatches retain
   their reservations and require the shared recovery protocol.

Workflow results, grouping plans, aggregation waves and post-curation evaluations
are linked by `proteus.combee.ace_batch@1`. Each sample's predictions, reflections,
tags and local candidate are recorded as `proteus.combee.ace_sample_learning@1`.

Offline validation occurs only when the last sample index of a batch is divisible
by `eval_steps`; the index resets each epoch. A short final batch does not imply
an extra validation. Strict improvement selects the best playbook, initially the
seed candidate, and hidden tests use that selection. Online windows are scored
before their learning batches, and batches never cross a window boundary.

## Configuration and extension

`CombeeOptions` shares ACE's evaluation/feedback protocol options, and adds
`sample_workflow`, `aggregation_stage`, `batch_size`, `max_workers` and `grouping`.
The workflow declares one reflection stage and the observed training pool. Its
round count, JSON mode and ground-truth policy must agree with the method.
Replacing the sample algorithm requires a compatible typed workflow and an
explicit extended profile. Curator prompts use an ordinary managed text stage.

Sample and aggregation maps have their own bounded waves, executed in sequence.
The shared run/role ledger bounds their actual calls. `execution.max_workers`
controls standalone baseline, post-curation, validation and test evaluations.
The installed fixture exposes both serial and parallel worker settings. Persistent
actor state is not required: these roles are fresh requests, and learning lives
in immutable playbook artifacts.

The method checkpoints at batch and online-window boundaries. It reuses the
per-operation journals for work inside a batch; recovery therefore does not
repeat completed generation, reflection or curator calls.

## Run the deterministic fixture

```sh
python -m proteus.examples.combee_ace --out /tmp/combee-ace --mode offline --workers 3
proteus recipe lock /tmp/combee-ace/recipe.json \
  --plugin proteus.methods.ace.batch:register \
  --plugin proteus.examples.ace_playbook:register \
  --source proteus=/absolute/path/to/Proteus --out /tmp/combee-ace/lock.json
proteus recipe run /tmp/combee-ace/recipe.json \
  --plugin proteus.methods.ace.batch:register \
  --plugin proteus.examples.ace_playbook:register \
  --lock /tmp/combee-ace/lock.json \
  --artifact-store /tmp/combee-ace/artifact-store --out /tmp/combee-ace/run
proteus recipe resume /tmp/combee-ace/recipe.json \
  --plugin proteus.methods.ace.batch:register \
  --plugin proteus.examples.ace_playbook:register \
  --lock /tmp/combee-ace/lock.json --out /tmp/combee-ace/run
```

Use a separate output directory with `--mode online` for the streaming fixture.
These examples perform no inference. Source-conformance tests execute the pinned
upstream sample and complete batch-controller ASTs with the original roles and
prompts, substituting model transport and logging. They compare complete prompt
multisets, per-task call ordering, final playbook text/IDs and candidate selection.
Synthetic conformance does not establish the published quality, cost or latency.

The method tests also cover real local HTTP transport, role/global concurrency
and call limits, hard exits after post-curation evaluation and batch checkpoints,
and restoration of failed batches. The wheel and sdist gate runs both modes with
one and three workers outside the source checkout, through the Python API and CLI.

Remaining Combee work includes dynamic batch profiling, the declared real-domain
experiment package, and same-budget quality/cost/wall-clock comparisons with ACE,
GEPA and naive parallel variants. Optional embedding-based deduplication and
upstream unbounded provider retries are outside this profile.
