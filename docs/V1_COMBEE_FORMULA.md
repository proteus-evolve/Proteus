# Combee and ACE on Formula (P08h)

For the accepted October configurations, actual launch scripts and result boundaries,
see the [frozen experiment guide](../recipes/combee/formula/README.md).
This page documents the general builder rather than a complete frozen run.

`python -m proteus.examples.combee_formula` prepares, runs, resumes and audits a
common-condition study over the pinned [ACE Formula dataset](V1_ACE_FORMULA.md).
The four configurable variants share actors, initial playbook, task order,
scoring rules and per-run budget ceilings:

| Variant | Learning and aggregation |
| --- | --- |
| `ace` | Original managed sequential ACE, one curator update per sample |
| `naive_parallel` | Parallel sample learning; one curator pass over all original reflections |
| `combee_fixed` | Fixed batches with augmented shuffle and map/reducer curation |
| `combee_dynamic` | The same Combee learning with the recorded throughput controller |

The naive arm uses `aggregation_mode: single_pass`, duplication factor 1 and one
group. It applies that curator's ADD proposal directly. Setting only the Combee
group count to 1 would still execute a reducer, so it is a different experiment.
The default `combee` aggregation mode keeps the existing reference behavior.

## Scope and data

This study uses the exact ACE 500/300/200 files and deterministic Formula parser
and metric. [Combee section 4.3](https://arxiv.org/html/2604.04247v2#S4.SS3) reports
250 Formula training samples; the exact paper subset has not been established.
The study therefore declares its 500-task common-condition profile and retains
full provenance. It does not label a guessed subset as the paper's data.

Both offline and online protocols are available. Offline scores the
validation-selected playbook on the full hidden test set. Online scores each
window before learning from it; that adaptive stream score is reported separately
from held-out accuracy. Each arm also measures its own empty-playbook baseline.

The default validation interval is 500, giving all batch schedules one full
validation at the end of each training epoch. An explicit different interval
retains the methods' original sample/batch-boundary rules and can create different
validation counts. Every validation cohort is included in the report and budget.

## Settings and commands

Start with the three chat actors and model settings described in
[ACE Formula](V1_ACE_FORMULA.md). The same settings JSON also accepts:

```json
{
  "variants": ["ace", "naive_parallel", "combee_fixed", "combee_dynamic"],
  "seeds": [0, 1, 2],
  "modes": ["offline", "online"],
  "eval_steps": 500,
  "curator_frequency": 1,
  "batch_size": 50,
  "max_workers": 8,
  "dynamic_batch": {"max_batch_size": 100}
}
```

This fragment must be combined with the required `generator`, `reflector` and
`curator` actor specifications. Credentials stay in their declared environment
variables. Preparation makes no inference calls:

```sh
python -m proteus.benchmarks.ace_formula_data \
  --out /tmp/formula-data --cache /tmp/formula-cache
python -m proteus.examples.combee_formula prepare \
  --data /tmp/formula-data --settings /path/to/combee-settings.json \
  --source /absolute/path/to/clean/Proteus --out /tmp/combee-formula
python -m proteus.examples.combee_formula run --out /tmp/combee-formula
python -m proteus.examples.combee_formula resume --out /tmp/combee-formula
python -m proteus.examples.combee_formula report --out /tmp/combee-formula
```

Preparation freezes every seed/mode/variant, source and plugin lock. Loading the
study reconstructs its expected configurations from the settings and rejects
drift before dispatch. Without an explicit `budget`, each mode uses a shared
conservative evaluation/model-call ceiling that accommodates all four arms.
An explicit budget must provide both ceilings. Actual consumption can differ;
equal ceilings do not establish equal cost. Exhausted or failed runs stop the
study and are never reported as completed experiments.

## Report and recovery

The private auditor regrades every complete cohort, checks the online denominator,
sample order, local counter updates, shared curation, selection and full rollout
coverage. It verifies reflection provenance and aggregator responses, ties the
dynamic decision to original timing evidence, and reconciles all model calls and
evaluations with the persistent ledger.

The output includes per-run accuracy, initial baseline, batch sizes, controller
observations, role calls, tokens, cost and end-to-end elapsed time, with per-mode
and per-variant multi-seed summaries. Missing cost stays unknown. Aggregates show
their known-observation count and do not silently average a smaller denominator.

End-to-end timing starts immediately before `run_recipe` and ends when it returns
a completed result. It includes baseline, training, validation, final evaluation
and persistence, but excludes data preparation and report auditing. It is distinct
from the sum of operation durations and from the paper's training latency. A
committed measurement survives resume unchanged; interruption before its receipt
leaves the full duration unknown, including when the run itself had completed.
Across a partially completed study, resume validates and reuses earlier finished
arms, continues the interrupted arm from committed operations, then starts the
remaining arms. A hard-exit check verifies that settled model calls and earlier
elapsed receipts are retained without another provider call.

Local HTTP fixtures and installed-package checks exercise the full study without
inference. Real model runs and quality/cost/elapsed comparisons remain required
for Combee's 1.0 acceptance. The separate GEPA combination remains available via
its [method recipe](V1_COMBEE_GEPA.md); this Formula experiment package uses ACE.
