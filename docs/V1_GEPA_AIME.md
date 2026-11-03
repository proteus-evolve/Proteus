# GEPA AIME study recipe (1.0 development)

For the accepted October seed-0 configuration, launchers and 23/30 → 24/30
result, see the [frozen experiment guide](../recipes/gepa/aime/README.md).
This page documents the general builder, not that run's complete configuration.

This recipe adapts the AIME example at GEPA commit
`8b0ce6cd99a234f6b74daf37558a2ac0ce18f975`. It supplies the complete pinned data,
solver format, mathematical feedback, reflective data, baseline and optimized
held-out evaluations, independent search seeds, recovery and an audited report.
It is an **adapted** profile. Completing the HTTP fixture or preparing the data
is not a real model experiment or a reproduction of published scores. R01/M2
remains open until the declared real experiment and its analysis are delivered.

## Prepare without calling a model

Install Proteus and the exact optional GEPA checkout described in
[V1_GEPA_SEARCH.md](V1_GEPA_SEARCH.md). Keep DSPy out of that environment: its
dependency pins conflict with this GEPA source revision. Use a clean committed
Proteus checkout for a real study; `prepare` refuses a dirty source pin.

Only dataset conversion needs PyArrow 21.0.0 (Python 3.12 is recommended):

```sh
python -m pip install pyarrow==21.0.0
python -m proteus.benchmarks.aime_data \
  --cache /tmp/aime-source-cache --out /tmp/aime-pinned-data
python -m proteus.examples.gepa_aime prepare \
  --data /tmp/aime-pinned-data --source "$PWD" \
  --settings recipes/gepa/aime/settings.json --out /tmp/aime-study
```

The source Parquet revisions and SHA-256 digests are fixed in
`proteus/benchmarks/aime_data.py`. All 90 training-source rows are shuffled once
by `random.Random(0)`, then split into 45 train and 45 validation examples; all
30 AIME 2025 test examples keep their original order. This split is independent
of each optimization seed. Task identity, source revision and private references
are preserved; a subject receives only the problem. The final JSON fingerprint is
`sha256:b083036dea44fdc3b025ac9b29f1198ff2529f1065cc8982f2c880542825a8cf`.

The datasets are not redistributed with the package. AI-MO/aimo-validation-aime
declares Apache-2.0; MathArena/aime_2025 declares **CC-BY-NC-SA-4.0**, independently
of Proteus's software license. The generated provenance file retains source/card
links, revisions, hashes, split identities and license notices. Preserve it with
the dataset and follow those terms when using or redistributing the data.

The supplied settings use the reference example's `gpt-4.1-mini` solver
(`temperature=1`, `max_tokens=32000`) and `gpt-5.1` reflection model. Credentials
are read from the named environment variable, never stored in settings or locks.
Copy settings outside the source tree to select another endpoint, credential
variable or model. Such changes are recorded; they do not establish equivalence
to the reference models. Only managed single-call chat actors are supported by
this recipe. Provider-reported token counts are retained; an unavailable price
remains unknown rather than becoming a zero-dollar run.

The default is three independent optimization seeds (0, 1, 2), minibatch size 3
and the upstream stopping threshold of 500 metric calls. GEPA checks its threshold
between iterations. Each optimized arm therefore reserves at most 550 optimization
trials plus 30 held-out trials, and a conservative 1,080 model-call cap including
reflection. Each baseline arm permits 30 trials/calls. These are caps, not reported
spend. The whole three-seed study's caps are 1,830 task evaluations and 3,330 model
calls. No implicit monetary cap is claimed. Seed controls search and task ordering,
not stochastic determinism at a remote model service.

## Run, resume and report

```sh
python -m proteus.examples.gepa_aime run --out /tmp/aime-study > /tmp/aime-report.json
python -m proteus.examples.gepa_aime resume --out /tmp/aime-study > /tmp/aime-report.json
python -m proteus.examples.gepa_aime report --out /tmp/aime-study > /tmp/aime-report.json
```

`prepare` freezes the dataset, provenance, settings, each resolved recipe and its
dependency lock before any inference. `run` refuses an existing run directory.
`resume` continues the same children, reuses committed operations and stops on
unresolved external calls or failed children; it does not create replacements.
A study lease prevents two controllers from advancing the same experiment.
Changing data, configuration, dependencies or controller code is a new experiment.

For each seed the baseline and optimized arms have separate manifests, journals,
ledgers and candidate stores. Only training/validation feedback enters GEPA.
Both held-out evaluations are hidden and selection-ineligible. The baseline is
tested first, but its result is never supplied to the optimizer or its selection
services. Every actor call has a fresh context. A missing baseline score stops
the study before spending the optimized arm's budget.

The report requires both complete arms for every declared seed. It checks each
manifest and result, the exact test cohort/order, receipt, candidate/artifact,
evaluation protocol, rollout binding and the deterministic math scores. It reads
the ledger in read-only mode and refuses unsettled work. It reports per-task and
per-seed accuracy, paired improvement, mean and sample standard deviation, actual
charged evaluations/model calls and known/unknown usage. A single seed has no
estimated sample standard deviation. Ledger wall time is the sum of charged
operation durations, not elapsed study time. The Python helper can exercise custom
fixtures, but their reports are explicitly labelled as non-AIME data; the public
preparation command accepts only the full pinned dataset.

## Domain semantics and declared adaptations

- The fixed text-only DSPy 3.4.1 `ChainOfThought(MathSolverSignature)` system/user
  formatting and string-field parser are preserved, including signature whitespace
  normalization. This is a small domain adapter, not a general DSPy replacement.
  The real pinned DSPy package is tested in a separate CI environment; no model
  calls occur during that check. Source licenses accompany the copied code.
- GEPA's exact AIME integer metric and feedback are used. Training feedback may
  include the training answer and worked solution, as in the reference. Hidden
  test answers and feedback do not reach reflection. A malformed generated answer
  is scored behavior; an HTTP/runtime failure is unavailable evidence, never zero.
- `proteus.aime_side_info` produces the reference's `score`, `input`, `output`,
  `reasoning`, `execution_feedback` records. `GepaOptions.dataset_stage` and
  `reflection_prompt_template` expose this domain behavior without a method-specific
  branch in the engine. `ReflectionBatch.from_examples()` preserves nested mapping
  order through canonical checkpoints, because GEPA renders fields in that order.
  A hard-exit test verifies identical reflection messages and call counts on resume.
- The profile keeps single mutation, full validation, strict improvement,
  epoch-shuffled minibatches, round-robin component selection and
  `skip_perfect_score=False`. The source AIME example defaults to hybrid frontier;
  it supplies no objective scores. Tests compare this adapter's reflection messages
  against the actual `optimize_anything` default over multiple iterations. This is
  not support for arbitrary objective-scored hybrid profiles or merge.
- Execution is serial and both model/evaluation caches are disabled. The source
  example enables evaluation caching and 32 workers. Repeated task occurrences
  therefore remain separately evaluated and charged here. Direct managed HTTP
  requests replace DSPy/LiteLLM transport: there is no implicit JSON-format fallback
  or SDK retry. Infrastructure errors stop the run. These choices can change cost,
  sampling and outcomes and are recorded in every recipe's deviations.

The CI gate proves source-format/metric conformance, fixed data integrity, HTTP
wiring, complete-report checks and recovery. Real benchmark evidence must name the
source/lock, models, seeds, complete split, budget, baseline, results and these
adaptations before the method's experimental milestone can be checked off.
