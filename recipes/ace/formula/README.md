# Standalone ACE / Formula

Two accepted profiles use the pinned upstream 500/300/200 Formula files, one training epoch and three refinement rounds. Offline H0/selected are 141/200 and 173/200; selection occurs at 200 training examples and differs from the final playbook. Online H0/stream accuracy are 141/200 and 163/200. Online predictions are evaluated before learning each window; this is not a held-out final-playbook score.

## Actual configurations

- [linux-ace-formula-offline-v2](../../experiments/single-seed-20261020/profiles/linux-ace-formula-offline-v2/): exact recipes/locks, scientific settings, source binding and historical launch command.
- [linux-ace-formula-online-v3](../../experiments/single-seed-20261020/profiles/linux-ace-formula-online-v3/): exact recipes/locks, scientific settings, source binding and historical launch command.

## Preparation and reproduction

Follow the [source reconstruction and new-run instructions](../../experiments/single-seed-20261020/README.md#prepare-a-new-aime-or-formula-study). Use the profile label above with `prepare_study.py`. Use the `api` reconstructed core. The actual study builder is `proteus.examples.ace_formula`. Preparation does not call a model; its `run` command does. New endpoint/model/path bindings require new locks.

## Recorded results

See [results.json](../../experiments/single-seed-20261020/results.json) and the [full profile index](../../experiments/single-seed-20261020/index.json). These are adapted single-seed results with the recorded limitations; they do not establish equality to published papers.
