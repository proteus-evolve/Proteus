# ACE Formula study (P06c)

For the accepted October configurations, actual launch scripts and result boundaries,
see the [frozen experiment guide](../recipes/ace/formula/README.md).
This page documents the general builder rather than a complete frozen run.

The Formula recipe runs the [ACE method](V1_ACE_METHOD.md) against its upstream
finance dataset. It prepares immutable studies, runs/resumes independent
replicates, and audits every prediction and score before returning a report.
This is an **adapted profile**, not a claim to reproduce published model scores.
Real provider inference and its resulting benchmark evidence remain a release
requirement; deterministic HTTP fixtures validate the implementation only.

## Reference and data

The source is [ace-agent/ace at
82709de050e1db6e6ef2f07bcb0393560b94992a](https://github.com/ace-agent/ace/tree/82709de050e1db6e6ef2f07bcb0393560b94992a).
The exact `eval/finance/data/` files supply 500 training, 300 validation and 200
test records. The downloader verifies each file's SHA-256, preserves line order,
checks for duplicate public inputs across splits, and verifies the complete
prepared dataset hash. `provenance.json` records URLs, source hashes, ordered task
IDs, processor hash and license provenance. Data is downloaded separately and is
not redistributed in the Proteus wheel or sdist. The upstream repository carries
Apache-2.0; these data files have no separate license declaration at this pin.

The public task contains only the reference processor's `question` and `context`;
the target remains in the private grading record. The upstream parser removes the
original wrapper and appends its exact numeric-answer instruction. The metric
removes commas and compares Python floats for equality; when conversion fails it
compares strings. No tolerance, answer reformatting or substitute grader is added.
Selected parser/metric source is covered by the bundled [ACE notice](../proteus/methods/ace/NOTICE.md).

FiNER is not included: its dataset-level accuracy counts individual labels, whereas
the current ACE method profile requires binary per-task scores. Its upstream
parsing also needs separate review. Formula support does not establish FiNER or
AppWorld support.

## Protocols

Each replicate starts with an empty playbook and its own hidden, whole-test
baseline evaluation. This is ACE's existing initial test; there is no redundant
second baseline run. The optimizer cannot read its scores.

| Mode | Learning and selection | Reported result |
| --- | --- | --- |
| Offline | Train in declared order; validate every `eval_steps` within each epoch; update the best only on strict improvement above the initial zero | Hidden test accuracy of that validation-selected candidate |
| Online | Use the official test pool as the stream; score a whole window with a fixed candidate before learning from any task in it | Task-weighted pre-learning window accuracy |

Online is adaptive evaluation, not performance on a held-out set untouched by
learning. Its hidden `stream` and observed `learn` evaluations deliberately name
the same ordered tasks. Offline train/validation/test remain distinct. No implicit
end-of-epoch validation is added: if no validation runs, or all validation scores
are zero, the selected offline candidate remains the initial playbook, as upstream.

The defaults follow the reference finance CLI: one offline epoch, three reflection
rounds on incorrect samples, curator frequency one, validation every 100 samples,
online windows of 15, and an 80000-token playbook prompt hint. Correct initial
answers still get one reflection, including when `max_num_rounds` is zero. The
reference Generator/Reflector/Curator prompts and post-curation observations are
preserved. See the method document for parser behavior and declared adaptations.

`num_epochs` controls offline only; online always makes one pass. Seeds label
independent replicates and appear in execution identities. The task order stays
fixed; this does not promise seeded or deterministic behavior from the provider.
The report aggregates offline and online separately with mean and sample SD.

## Prepare without inference

```bash
python -m proteus.benchmarks.ace_formula_data \
  --cache /tmp/ace-formula-cache --out runs/ace-formula-data
```

An optional `--source /path/to/pinned/ace` reads the same source files locally;
byte checks still apply. Reusing a correct cache is allowed; corruption fails.

Write `runs/formula-settings.json` with explicit provider and model settings. Replace
the example endpoint/model names with your provider's actual values. Keys stay in
the named environment variable, not the file:

```json
{
  "generator": {
    "runtime": {"plugin": {"name": "proteus.chat", "version": "1"},
      "options": {"base_url": "https://YOUR_PROVIDER/v1", "key_env": "FORMULA_API_KEY"}},
    "model": "YOUR_MODEL", "model_settings": {"temperature": 0, "max_tokens": 4096}
  },
  "reflector": {
    "runtime": {"plugin": {"name": "proteus.chat", "version": "1"},
      "options": {"base_url": "https://YOUR_PROVIDER/v1", "key_env": "FORMULA_API_KEY"}},
    "model": "YOUR_MODEL", "model_settings": {"temperature": 0, "max_tokens": 4096}
  },
  "curator": {
    "runtime": {"plugin": {"name": "proteus.chat", "version": "1"},
      "options": {"base_url": "https://YOUR_PROVIDER/v1", "key_env": "FORMULA_API_KEY"}},
    "model": "YOUR_MODEL", "model_settings": {"temperature": 0, "max_tokens": 4096}
  },
  "seeds": [0, 1, 2], "modes": ["offline", "online"]
}
```

Each role may use a different explicit model. The study currently requires fresh
`proteus.chat` actors without tools or extra static prompts. Enabling `json_mode`
also requires `response_format: {"type": "json_object"}` in all three actors'
model settings. `no_ground_truth` removes the target from reflection prompts;
the grader still computes correctness.

From a clean, committed Proteus checkout:

```bash
python -m proteus.examples.ace_formula prepare \
  --data runs/ace-formula-data --settings runs/formula-settings.json \
  --source . --out runs/ace-formula-study
python -m proteus.examples.ace_formula run --out runs/ace-formula-study
python -m proteus.examples.ace_formula report --out runs/ace-formula-study \
  > runs/ace-formula-report.json
python -m proteus.examples.ace_formula resume --out runs/ace-formula-study
```

The CLI requires the full pinned dataset. The Python API accepts custom datasets,
which the report explicitly labels as custom/fixture. Preparation writes resolved
recipe/lock files for every seed/mode, the empty artifact, data and provenance.
No model call occurs until `run`. A changed controller, dataset, recipe, lock or
provenance fails validation; use the original recorded source for resume/report.
Do not overwrite a failed run to obtain a different result.

The full default dataset has conservative per-replicate bounds of 4400 subject
evaluations / 6400 total model calls offline and 1400 / 2200 online. These are hard
upper bounds, not expected spend: successful answers reduce regeneration calls.
Three replicates of both modes permit up to 25800 total model calls. Call budgets
do not imply a dollar cap. The report retains actual or unknown cost and token
usage, including every role; charged operation time is not study elapsed time.

## Evidence and validation

The private report uses shared `RunAudit` helpers, also used by the GEPA AIME
report. It verifies candidate/store identity, exact task cohorts, input-bound
protocols, raw predictions, hidden feedback rules and deterministic regrading.
Reporting checks plugin implementation/schema identities against the recorded lock
even when reading on a different host; changed grading code cannot silently
reinterpret an old experiment. The current domain metrics use only the standard library.
Formula additionally checks every training observation, sample order, regeneration
count, validation selection, window boundaries, complete chronological rollout
coverage and role/ledger totals. A missing score never shrinks the denominator or
becomes a valid zero; unreported evaluation cohorts are rejected. It audits each completed run before starting the next one.
Reports are read-only analyses; they are never fed back into optimization.

CI verifies all 1000 records against the actual pinned processor, compares complete
offline/online prompts with the actual upstream algorithm on HTTP fixtures, tests
protocol drift and unavailable predictions, and exercises hard-exit recovery after
window scoring. Installed wheel and sdist tests run both studies and resume from
outside the checkout. These checks establish the declared implementation protocol;
they are not real-model evidence of performance improvement.
