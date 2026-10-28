# GEPA / AIME

## Sources and citation

Original method: Agrawal et al., [GEPA: Reflective Prompt Evolution Can Outperform
Reinforcement Learning](https://arxiv.org/abs/2507.19457v2).
Official code: [gepa-ai/gepa](https://github.com/gepa-ai/gepa), pinned to
[`8b0ce6cd99a234f6b74daf37558a2ac0ce18f975`](https://github.com/gepa-ai/gepa/tree/8b0ce6cd99a234f6b74daf37558a2ac0ce18f975).
Use `agrawal2026gepareflectivepromptevolution` from [references.bib](../../../references.bib).
See [source attribution and deviations](../../../docs/METHOD_REFERENCES.md).

## Experiment setup

The accepted seed-0 run uses 45 train / 45 validation / 30 private tasks, a metric-call budget of 500 and minibatches of 3. Solver/reflection use temperature 0, disabled thinking and max_tokens 8192. Baseline is 23/30; the selected candidate is 24/30. The existing settings.json beside this README remains an illustrative example. The separate full GEPA freeze is recorded in [research PR 38](https://github.com/proteus-evolve/Proteus-RSI-Research/pull/38).

## Actual configurations

- [linux-aime-full-seed-0-v2](../../experiments/single-seed-20261020/profiles/linux-aime-full-seed-0-v2/): exact recipes/locks, scientific settings, source binding and historical launch command.

## Preparation and reproduction

Follow the [source reconstruction and new-run instructions](../../experiments/single-seed-20261020/README.md#prepare-a-new-aime-or-formula-study). Use the profile label above with `prepare_study.py`. Use the `api` reconstructed core. The actual study builder is `proteus.examples.gepa_aime`. Preparation does not call a model; its `run` command does. New endpoint/model/path bindings require new locks.

## Recorded results

See [results.json](../../experiments/single-seed-20261020/results.json) and the [full profile index](../../experiments/single-seed-20261020/index.json). These are adapted single-seed results with the recorded limitations; they do not establish equality to published papers.
