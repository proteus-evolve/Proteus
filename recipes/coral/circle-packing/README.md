# CORAL-inspired circle packing

## Sources and citation

Original method: Qu et al., [CORAL: Towards Autonomous Multi-Agent Evolution for
Open-Ended Discovery](https://arxiv.org/abs/2604.01658v3).
Official code: [Human-Agent-Society/CORAL](https://github.com/Human-Agent-Society/CORAL),
pinned to [`0123dfb939b35228cf2c1fde224cd0561e727408`](https://github.com/Human-Agent-Society/CORAL/tree/0123dfb939b35228cf2c1fde224cd0561e727408).
Use `qu2026coralautonomousmultiagentevolution` from [references.bib](../../../references.bib),
which preserves the full author list.
See [method/task attribution and deviations](../../../docs/METHOD_REFERENCES.md).

## Experiment setup

Both final arms optimize the original 26-circle objective from the same H0, with exactly 20 proposals: one worker × 20 or two workers × 10. There is one evaluation worker. Independent geometry checks confirm selected radius sums of 2.4480910215 and 2.3729279721. Proposal outcomes include 8/20 and 9/20 timeouts, respectively; missing scores are not zero. Confirmation is the same public problem with a fresh trial seed, not held-out generalization. The runtime uses archived JSON chat workers and released feedback, not the paper's persistent native coding agents.

## Actual configurations

- [linux-coral-circle-1-workers-v5-1](../../experiments/single-seed-20261020/profiles/linux-coral-circle-1-workers-v5-1/): exact recipes/locks, source binding and historical launch command.
- [linux-coral-circle-2-workers-v5-1](../../experiments/single-seed-20261020/profiles/linux-coral-circle-2-workers-v5-1/): exact recipes/locks, source binding and historical launch command.

## Preparation and reproduction

The [historical launchers and bindings](../../experiments/single-seed-20261020/historical-builders/) include `prepare_coral_output_channels_v5_1.py, launch_coral_output_channels_v5_1.py, circle_linux_worker_v4_1.py and circle_released_feedback_v4_1.py`. Inspect each profile's `launch.json` for its original command. These scripts retain the original qualified Linux host paths and scoped relay; they are not a portable one-command rerun.

For a fresh study, use the [framework guide](../../../docs/V1_CORAL_CIRCLE_PACKING.md) and provide the actual container/runtime/actor inputs. Inspect the builder arguments with `python -m proteus.benchmarks.coral_circle_packing.recipe --help`. The [central guide](../../experiments/single-seed-20261020/README.md) explains the exact source trees and historical-runtime boundary.

## Recorded results

See [results.json](../../experiments/single-seed-20261020/results.json) and the [full profile index](../../experiments/single-seed-20261020/index.json). These are adapted single-seed results with the recorded limitations; they do not establish equality to published papers.
