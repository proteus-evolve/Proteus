# Frozen full single-seed experiment recipes

For the original method papers, official code repositories, exact upstream
commits and adaptation boundaries, see [method references](../../../docs/METHOD_REFERENCES.md)
and [references.bib](../../../references.bib). The [family guides](../../README.md)
include citations for each experiment; Combee/Formula cites both Combee and ACE,
and Native Pi credits the upstream software.

These are the actual controls for **15 experimental profiles / 16 recipe files**
started on 20 October 2026 and accepted by the final 23 October evidence cut.
GEPA has separate baseline and optimized recipes; Meta-Harness covers three
domains in one profile. These are not 15 independent benchmarks.

The public development branch previously exposed the builders under
`proteus/examples/`, but only illustrative AIME settings under `recipes/`.
This directory connects the actual configurations, launchers, source versions
and results. All documentation here is in English.

## Inspect the exact controls

From the repository root, with Python 3.12 or later:

```sh
python recipes/experiments/single-seed-20261020/inspect_experiment.py
python recipes/experiments/single-seed-20261020/inspect_experiment.py --verify
python recipes/experiments/single-seed-20261020/inspect_experiment.py linux-ace-formula-offline-v2
```

These commands do not import a model/container runtime or make provider calls.
Verification checks all 16 canonical configuration and lock hashes and every
file in `SHA256SUMS`. Each `profiles/<label>/` contains the byte-preserved
historical recipes and locks, the scientific settings, source bindings and
`launch.json` recording the historical commands. Derived indexes/settings are
explicitly separate from those immutable controls.

| Profile group | Conditions | Setup and result scope |
| --- | ---: | --- |
| GEPA/AIME | 1 | Seed 0; 45/45/30 tasks; baseline 23/30, selected 24/30 |
| Standalone ACE/Formula | 2 | Offline 141/200 → 173/200; online 141/200 → 163/200 |
| Combee/Formula | 8 | ACE/naive/fixed/dynamic × offline/online; see family guide |
| Meta-Harness-inspired | 1 | Three domains; two baselines + three generated programs; four systems selected for private evaluation |
| CORAL-inspired | 2 | 20 proposals per arm; one proposer vs two × 10; radius sums 2.4481 / 2.3729 |
| Native Pi | 1 | Two episodes; accepted source activates in EP2; completed-run resume adds no work |

The machine-readable [results](results.json) preserve count denominators,
selected/last identities, online metric semantics, time/cost missingness and
single-seed limitations. The [profile index](index.json) binds results to the
actual recipes. No experiment was rerun to create this directory.

## Reconstruct the source that ran

The historical scientific cores are **not the unmodified `dev/1.0.0` checkout**.
They used Linux transport, ledger and output-channel overlays. Do not copy an
old lock onto current source and claim exact reproduction.

[sources.json](sources.json) gives the public base, ordered patches, historical
revision and exact Git tree for five cores. For example:

```sh
python recipes/experiments/single-seed-20261020/rebuild_source.py api \
  --out /tmp/proteus-api-frozen
python recipes/experiments/single-seed-20261020/rebuild_source.py api-ledger-serialized-io-v1 \
  --out /tmp/proteus-parallel-frozen
```

The helper reads the base object from this local repository; it does not fetch,
install packages or call models. The base commit must be available locally
(`6a718611973c261f0b661ee3dd3b8a3505d74586`; fetch it from origin if necessary).
It refuses an existing output directory, applies the exact overlays, checks the
complete Git tree, then creates a **new local commit**. This proves source-byte
and file-mode equivalence, not the original commit identity or runtime history.

| Profiles | Core |
| --- | --- |
| AIME, standalone ACE, Combee ACE controls | `api` |
| Combee naive/fixed/dynamic, including fresh-H0 Naive v7 | `api-ledger-serialized-io-v1` |
| Meta-Harness | `mce` |
| CORAL v5.1 final arms | `coral-output-channels-v5_1` |
| Native Pi | `coral` (historical revision `722ac71a…`) plus the native binding |

The four offline Formula continuations additionally used separately pinned
operators: their original recipe/lock/study and branch manifests are in
`continuations/`. The source tree reconstruction does **not** recreate parent
databases, operational continuation commits, recovery timing or original bills.
The MCE repair and final reporter likewise have separate provenance; see its
family guide. These runtime histories are not silently merged into the public
framework by adding this archive.

## Prepare a new AIME or Formula study

Use a new output directory and the reconstructed core matching the table above.
Install that source's dependencies in a dedicated environment; GEPA additionally
requires its [pinned optimizer dependency](../../../docs/V1_GEPA_METHOD.md).
No API key is required for preparation. Source datasets are downloaded with their
original hashes rather than redistributed here:

```sh
export PYTHONPATH=/tmp/proteus-api-frozen
python -m proteus.benchmarks.ace_formula_data \
  --out /tmp/proteus-formula-data --cache /tmp/proteus-data-cache
# AIME instead requires pyarrow==21.0.0:
# python -m proteus.benchmarks.aime_data --out /tmp/proteus-aime-data --cache /tmp/proteus-data-cache

python recipes/experiments/single-seed-20261020/prepare_study.py \
  linux-ace-formula-offline-v2 --source /tmp/proteus-api-frozen \
  --data /tmp/proteus-formula-data --out /tmp/new-ace-offline \
  --base-url https://api.deepseek.com/v1 --model deepseek-chat \
  --key-env DEEPSEEK_API_KEY
```

The preparer preserves the recorded scientific settings and explicitly replaces
the old host-bound relay/guard with the endpoint and key-environment name supplied
by the caller. The original `deepseek-flash` relay alias is preserved unless
`--model` is supplied; the example intentionally names a direct-provider model.
Changing transport, source commit, model identity or paths produces a **new
recipe/lock identity**, not a replay of the original experiment. Provider outputs
and dynamic timing need not reproduce the historical numbers.

Preparation prints the exact run command. For the example above, after putting
credentials in the named environment variable, execution is:

```sh
PYTHONPATH=/tmp/proteus-api-frozen python -m proteus.examples.ace_formula \
  run --out /tmp/new-ace-offline
```

Only `prepare` was used in offline qualification of these new wrappers. `run`
makes new paid requests; this repository update does not execute it. Use the
same preparer for the other AIME/Formula profile labels listed by `inspect_experiment.py`.

## Container profiles and historical launch scripts

CORAL, MCE and Native Pi are not generic chat-only studies. Their actual
[`historical-builders/`](historical-builders/) scripts are included byte-for-byte,
including worker/binding helpers; each profile's `launch.json` records the actual
entry command. They refer to the original Linux root, scoped relay, admission
controls and qualified Harbor/Docker installation. They are **historical launch
scripts, not portable one-command launchers**. Do not execute them from this
archive and assume the old host or private controls exist.

For a new container study, follow the linked family guide, rebuild the matching
core, provide a fresh compatible model/proposer/worker configuration and Harbor
runtime, and regenerate the recipe/lock with its public builder. Native additionally
requires the upstream Pi source image and the archived native transport/binding;
its acceptance is a source-activation check, not a benchmark score. The exact
historical controls are available here even where turnkey runtime re-creation is
not provided.

## Final replacements and interpretation

- **Naive offline:** the accepted result belongs to
  `linux-combee-formula-offline-naive_parallel-v7-fresh-H0`, not the retired v5
  profile in the older bundle. v7 starts from original H0; it does not resume a
  learned v5 memory. Its actual recipe/lock and settings replace v5 in this index.
- **Offline Formula:** 500 training / 300 visible / 200 private tasks. Selected
  need not equal last; standalone ACE selects after 200 training examples even
  though training continues to 500. Private results do not choose the candidate.
- **Online Formula:** scores are predictions made before learning each test
  window, not untouched evaluation of the final playbook.
- **Meta-Harness:** the memory/data split seed is 42; the outer recipe seed is 0.
  The one-call/three-program proposer is an adaptation, not the paper's long
  coding-agent search. Added context is measured in characters.
- **CORAL:** final confirmation reruns the same packing task. Neither held-out
  generalization, an isolated speedup nor a significant two-worker advantage is
  established.
- **Native:** real TypeScript modification, boundary build and EP2 activation
  pass. All eight raw phase codes remain 137 and canonical usage remains unknown;
  functional acceptance is not a claim of clean phase exits. Resume evidence is
  limited to an already completed run.

All quantitative results are adapted single-seed observations. Historical
failures, unknown measurements and interrupted/continued execution are retained
as limitations. No learned per-episode harnesses, raw model outputs, private
answer datasets, credentials or scientific/financial databases are included.
