# Recipes

Use the [frozen single-seed experiment recipes](experiments/single-seed-20261020/README.md)
for the actual October 2026 results: GEPA/AIME, standalone ACE, all eight Combee
conditions, Meta-Harness, CORAL with one/two proposers, and Native Pi.
That index includes the final Naive offline v7 replacement, the actual JSON
recipes/locks, settings, historical launch scripts, source patches and result
boundaries. It is separate from the small illustrative configurations below.

| Family | Experiment guide |
| --- | --- |
| GEPA / AIME | [Recipe and results](gepa/aime/README.md) |
| ACE / Formula | [Offline and online](ace/formula/README.md) |
| Combee / Formula | [Eight conditions](combee/formula/README.md) |
| Meta-Harness | [Three-domain study](meta-harness/mce/README.md) |
| CORAL | [One/two proposers](coral/circle-packing/README.md) |
| Native Pi | [Source activation](native/pi/README.md) |

For new experiments, start with the framework's
[builder catalog](../docs/V1_RECIPE_CATALOG.md). A builder is an implementation
entry point; it does not identify the settings, source revision or results of
the frozen study. `gepa/aime/settings.json` and `native/minimal.toml` remain
illustrative examples rather than the configurations that produced these results.
