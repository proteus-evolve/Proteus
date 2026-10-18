# Compare explicit conditions and aggregate seeds

`proteus recipe summarize STUDY_JSON --out NEW_JSON` reads existing settled runs.
It does not run a method or model. Add `--include-private` to compute quality from
hidden final grades in an owner-side offline report. Without that flag, hidden
quality remains unavailable, including aggregate means and paired differences.

## Executable configuration-only ablation

```sh
python -m proteus.examples.search_policy_study --out /tmp/policy-study --seeds 0 1
```

This prepares four recipes: mean and worst-task final selection for each of two
seeds. `experiments.json` lists each resolved recipe and its check/lock/run/resume
commands. Replace `<PROTEUS_GIT_ROOT>` with the matching source checkout and run
the listed commands. Preparation neither invents a source lock nor runs a model.
Outputs should be outside the source checkout or in an ignored directory.

```sh
proteus recipe summarize /tmp/policy-study/study.json \
  --include-private --out /tmp/policy-study/comparison.json
```

Both conditions evaluate the same fixed candidates and validation scores, then
perform the private final evaluation. Only `/search/final` varies. The deliberate
fixture produces final scores 0.8 and 0.4, respectively, with a paired difference
of -0.4. Repeating seeds exercises aggregation; it is not evidence of statistical
model performance or a recommendation of either policy.

## Declare the comparison

`StudySpec` contains a title, mode, expected seeds, cases, aggregation rule and
optional baseline. Each case identifies its condition, run directory, actual
recipe seed and final evaluation IDs. Run paths are relative to the study file.
An optional candidate ID disambiguates a named cohort; otherwise each evaluation
must contain exactly one complete candidate cohort. Summaries use final-purpose
`test` evaluations. Such an evaluation can be same-problem confirmation, as in
circle packing; it does not automatically imply a held-out task.

- `controlled` requires resolved recipes to match except names, declared seeds,
  pinned JSON data locations and explicit `varied_fields`. These fields use
  slash-separated object keys/list indices, such as `/search/final` or
  `/budget/max_evaluations`. Escaped JSON-pointer tokens are unsupported.
- A variation must exist in every case. Final task occurrences, dataset identity,
  evaluator implementation/settings, state policy and feedback timing must still
  match between conditions for each seed. Masking a broad config field cannot
  bypass this check. Source and package pins, and all shared plugin identities,
  must also match.
- `reference_profiles` preserves each method's declared reference setting and
  deviations. It emits descriptive summaries without a controlled-comparison
  claim, a variation mask or paired baseline deltas.

Every condition requires every declared seed exactly once. A copied/resumed run
cannot count as another observation. Within a condition, source/plugin/package locks must remain identical, and configuration may differ
only in the declared replicate seeds and pinned JSON data locations. Unknown or
hidden measurements make the whole mean unavailable rather than dropping runs.
Sample standard deviation is reported only with at least two known values.

`task_mean` pools scores across the explicitly selected final cohorts;
`cohort_mean` weights each selected cohort equally. Neither pools undeclared
validation rounds. The result retains each manifest/report hash, selected cohort,
reference profile and declared variation hashes. Baseline differences are paired
by seed. Cost and time statistics retain the semantics of
[V1_RESULT_EXPORT.md](V1_RESULT_EXPORT.md): summed operation time and ledger event
span are distinct from measured active study runtime.

The comparison does not upgrade reference status, regrade metrics, or assert that
all controlled experiments use equal budgets when budget is an explicitly varied
field. Original-domain study reports remain responsible for their additional
protocol and timing audits. Real-model evidence and full release acceptance are
still required for the five method families.
