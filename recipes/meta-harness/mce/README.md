# Meta-Harness-inspired three-domain study

## Sources and citation

Original method: Lee et al., [Meta-Harness: End-to-End Optimization of Model
Harnesses](https://arxiv.org/abs/2603.28052v1).
Official code: [stanford-iris-lab/meta-harness](https://github.com/stanford-iris-lab/meta-harness),
pinned to [`8123ccabe2b19fa1123090b2e5f1bacc20963ce8`](https://github.com/stanford-iris-lab/meta-harness/tree/8123ccabe2b19fa1123090b2e5f1bacc20963ce8).
Use `lee2026metaharnessendtoendoptimizationmodel` from [references.bib](../../../references.bib).
See [source attribution and deviations](../../../docs/METHOD_REFERENCES.md).

## Experiment setup

The original profile uses USPTO, Symptom2Disease and LawBench, split seed 42 and outer recipe seed 0. It evaluates NoMemory, FewShotAll and three actual generated programs. Each system has 450 learning callbacks and 260 visible predictions; four selected systems cover 412 private tasks each. The proposer uses one JSON call producing three programs, rather than the paper's long coding-agent search. The final operational recovery fixes and reporting acceptance are separate from the original scientific core. A fresh run does not require replaying those failures, and an unchanged raw recipe alone does not reconstruct their runtime history.

## Actual configurations

- [linux-mce-full-three-domains-seed42-v1](../../experiments/single-seed-20261020/profiles/linux-mce-full-three-domains-seed42-v1/): exact recipes/locks, source binding and historical launch command.

## Preparation and reproduction

The [historical launchers and bindings](../../experiments/single-seed-20261020/historical-builders/) include `run_mce_formal.py and live_workspace_actor.py`. Inspect each profile's `launch.json` for its original command. These scripts retain the original qualified Linux host paths and scoped relay; they are not a portable one-command rerun.

For a fresh study, use the [framework guide](../../../docs/V1_META_HARNESS.md) and provide the actual container/runtime/actor inputs. Inspect the builder arguments with `python -m proteus.examples.meta_harness --help`. The [central guide](../../experiments/single-seed-20261020/README.md) explains the exact source trees and historical-runtime boundary.

## Recorded results

See [results.json](../../experiments/single-seed-20261020/results.json) and the [full profile index](../../experiments/single-seed-20261020/index.json). These are adapted single-seed results with the recorded limitations; they do not establish equality to published papers.
