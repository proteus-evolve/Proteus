# Combee / Formula — eight conditions

ACE, naive_parallel, combee_fixed and combee_dynamic each have offline and online profiles. Parallel conditions use eight workers, a fixed batch of 50 or the recorded dynamic controller. Offline Naive uses the final fresh-H0 v7 replacement. Dynamic batch sizes depend on measured latency; the historical offline run mostly falls back to batch 1. The runs share host resources and include continuations, so no isolated speedup or original-paper efficiency result is established.

## Actual configurations

- [linux-combee-formula-offline-ace-v2](../../experiments/single-seed-20261020/profiles/linux-combee-formula-offline-ace-v2/): exact recipes/locks, scientific settings, source binding and historical launch command.
- [linux-combee-formula-online-ace-v2](../../experiments/single-seed-20261020/profiles/linux-combee-formula-online-ace-v2/): exact recipes/locks, scientific settings, source binding and historical launch command.
- [linux-combee-formula-offline-naive_parallel-v7-fresh-H0](../../experiments/single-seed-20261020/profiles/linux-combee-formula-offline-naive_parallel-v7-fresh-H0/): exact recipes/locks, scientific settings, source binding and historical launch command.
- [linux-combee-formula-online-naive_parallel-v5-ledger-io](../../experiments/single-seed-20261020/profiles/linux-combee-formula-online-naive_parallel-v5-ledger-io/): exact recipes/locks, scientific settings, source binding and historical launch command.
- [linux-combee-formula-offline-combee_fixed-v5-ledger-io](../../experiments/single-seed-20261020/profiles/linux-combee-formula-offline-combee_fixed-v5-ledger-io/): exact recipes/locks, scientific settings, source binding and historical launch command.
- [linux-combee-formula-online-combee_fixed-v6-ledger-io](../../experiments/single-seed-20261020/profiles/linux-combee-formula-online-combee_fixed-v6-ledger-io/): exact recipes/locks, scientific settings, source binding and historical launch command.
- [linux-combee-formula-offline-combee_dynamic-v6-ledger-io](../../experiments/single-seed-20261020/profiles/linux-combee-formula-offline-combee_dynamic-v6-ledger-io/): exact recipes/locks, scientific settings, source binding and historical launch command.
- [linux-combee-formula-online-combee_dynamic-v6-ledger-io](../../experiments/single-seed-20261020/profiles/linux-combee-formula-online-combee_dynamic-v6-ledger-io/): exact recipes/locks, scientific settings, source binding and historical launch command.

## Preparation and reproduction

Follow the [source reconstruction and new-run instructions](../../experiments/single-seed-20261020/README.md#prepare-a-new-aime-or-formula-study). Use the profile label above with `prepare_study.py`. For Combee ACE controls use `api`; for naive/fixed/dynamic use `api-ledger-serialized-io-v1`. The actual study builder is `proteus.examples.combee_formula`. Preparation does not call a model; its `run` command does. New endpoint/model/path bindings require new locks.

## Recorded results

See [results.json](../../experiments/single-seed-20261020/results.json) and the [full profile index](../../experiments/single-seed-20261020/index.json). These are adapted single-seed results with the recorded limitations; they do not establish equality to published papers.
