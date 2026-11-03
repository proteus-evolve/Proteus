# Native Pi — source activation

## Software source and citation

This integration uses Mario Zechner and contributors' **Pi Agent Harness v0.85.1**:
[official repository](https://github.com/earendil-works/pi), pinned to
[`d981de1229ef899957bbe968bc8dcda02a21f477`](https://github.com/earendil-works/pi/tree/d981de1229ef899957bbe968bc8dcda02a21f477),
under the [upstream MIT license](https://github.com/earendil-works/pi/blob/d981de1229ef899957bbe968bc8dcda02a21f477/LICENSE).
Use the software entry `zechner_pi_0851` from [references.bib](../../../references.bib).
No separate Pi method paper is asserted. See [software attribution](../../../docs/METHOD_REFERENCES.md#native-pi-software-credit).

## Experiment setup

This is an integration experiment, not a quality benchmark. It uses upstream Pi 0.85.1 at d981de1229ef899957bbe968bc8dcda02a21f477, one seed and two episodes, with observe/propose/act/reflect and configured phase budgets 8/6/20/6. EP1 adds --proteus-evolution-marker to the real TypeScript CLI; the accepted snapshot is built and activated in EP2. The compiled entry prints PROTEUS_NATIVE_EVOLVED_V1 and exits 0 without a model request. Evaluators are empty and selection is none. All eight raw phase codes remain 137; functional acceptance does not imply clean phase exits. The external ledger records 37 model calls; completed-only resume adds zero calls/container work. This is distinct from the legacy Pi 0.84.2 instructions in docs/RECIPES.md.

## Actual configurations

- [linux-native-pi-upstream-source-activation-v1](../../experiments/single-seed-20261020/profiles/linux-native-pi-upstream-source-activation-v1/): exact recipes/locks, source binding and historical launch command.

## Preparation and reproduction

The [historical launchers and bindings](../../experiments/single-seed-20261020/historical-builders/) include `prepare_native_upstream.py, run_native_formal.py, native_linux_binding_v2.py and linux_native_sse.py`. Inspect each profile's `launch.json` for its original command. These scripts retain the original qualified Linux host paths and scoped relay; they are not a portable one-command rerun.

For a fresh study, use the [framework guide](../../../docs/V1_NATIVE_INPUTS.md) and provide the actual container/runtime/actor inputs. The custom `BudgetedPi` binding and upstream source image must be qualified; the minimal native example is not a substitute. The [central guide](../../experiments/single-seed-20261020/README.md) explains the exact source trees and historical-runtime boundary.

## Recorded results

See [results.json](../../experiments/single-seed-20261020/results.json) and the [full profile index](../../experiments/single-seed-20261020/index.json). These are adapted single-seed results with the recorded limitations; they do not establish equality to published papers.
