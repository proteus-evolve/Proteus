# CORAL circle-packing recipe

This experimental domain adapter uses the original 26-circle packing seed and
task from CORAL `0123dfb939b35228cf2c1fde224cd0561e727408`. It prepares an
unmodified-seed baseline or the same optimization task with one or two persistent
research workers. Actual model optimization results remain a release requirement.

## Domain and reproducibility

The objective is to maximize the sum of 26 radii inside a unit square. The score
is the verified sum divided by **2.635977**, retaining the reference's **1e-6**
boundary and overlap tolerance. The original seed's sum is approximately
0.959764217, with score 0.364101894; this is a numeric baseline, not a research
result. A candidate's claimed sum or score is ignored.

The reference files, Apache-2.0 license, source paths and hashes are shipped in
`proteus.benchmarks.coral_circle_packing`. NumPy 2.2.6 and SciPy 1.15.3 CPython
3.12 Linux amd64 wheels are pinned by PyPI hashes. A recorded source build
installs them under the candidate image's `.proteus-deps/` directory, without
changing the separate verifier image. The first build requires network access;
subsequent identical candidate builds use the existing verified run-local cache.
Build cost and time remain in the common ledger.

Candidate code exports `packing.json` into Harbor's artifact channel. A separate
private container checks the JSON geometry using only stdlib arithmetic. It
does not import candidate code, use candidate-modified NumPy, or trust a forged
reward file. Published task-tree hashes bind the verifier and instructions.

Declared adaptations include independent JSON verification, a 256,000-byte
geometry limit, explicit Python/NumPy run seeds, pinned dependencies and possible
last-bit differences from NumPy reductions. The final hidden confirmation
repeats the same optimization problem with a fresh trial seed: **it is not a
held-out task or a generalization estimate**. See the packaged `NOTICE.md`.

## Prepare and run the original seed

The external Harbor installation must match the pinned runtime described in
[V1_HARBOR.md](V1_HARBOR.md). Preparation does not run candidates or call models.

```sh
python -m proteus.benchmarks.coral_circle_packing.recipe \
  --python /absolute/path/to/harbor/bin/python --out packing-baseline
proteus recipe lock packing-baseline/recipe.json \
  --plugin proteus.benchmarks.coral_circle_packing.recipe:register \
  --source proteus=/absolute/path/to/Proteus --out packing-baseline/lock.json
proteus recipe run packing-baseline/recipe.json \
  --plugin proteus.benchmarks.coral_circle_packing.recipe:register \
  --lock packing-baseline/lock.json --artifact-store packing-baseline/artifact-store \
  --out packing-baseline/run
```

The baseline evaluates the unmodified seed once for observed optimization and
once for hidden confirmation. The subject timeout is 600 seconds per trial.

## Prepare one- and two-worker conditions

Supply a `PluginSpec` JSON file for an already configured worker runtime. A
trusted local Codex runtime can be declared without reading a credential:

```python
import hashlib
from pathlib import Path
from proteus.core.contracts import PluginRef
from proteus.recipes.config import PluginSpec
from proteus.workers.codex import codex_worker

binary = Path('/absolute/path/to/codex')
options = codex_worker((str(binary),), model='YOUR_MODEL',
    command_files={str(binary): 'sha256:' + hashlib.sha256(binary.read_bytes()).hexdigest()},
    api_base_url='https://YOUR_RESPONSES_ENDPOINT/v1',
    api_key_env='CODEX_API_KEY', credential_env=('CODEX_API_KEY',),
    network_access=True, timeout_seconds=3600)
Path('worker.json').write_text(PluginSpec(
    plugin=PluginRef(name='proteus.worker.command', version='1'),
    options=options.to_dict()).to_json())
```

The actual CLI and endpoint must be verified for the selected deployment. This
local command profile is trusted and does not isolate the research agent from
host files. Use the explicit `proteus.worker.harbor` declaration from
[V1_HARBOR_WORKERS.md](V1_HARBOR_WORKERS.md) for a containerized provider;
its image must contain the provider executable and its exact command pins.

```sh
python -m proteus.benchmarks.coral_circle_packing.recipe \
  --python /absolute/path/to/harbor/bin/python --worker-spec worker.json \
  --workers 1 --submissions 20 --seed 0 --out packing-one
python -m proteus.benchmarks.coral_circle_packing.recipe \
  --python /absolute/path/to/harbor/bin/python --worker-spec worker.json \
  --workers 2 --submissions 20 --seed 0 --out packing-two
```

Lock and run each recipe as above. Both conditions allow 20 processed formal
submissions plus the initial baseline and final confirmation. More workers do
not multiply the formal evaluation allowance. A submission cap does not enforce
equal model-token or monetary cost: current provider usage may be unknown, and
those comparisons must use measured receipts. Do not interpret equal evaluation
counts as equal monetary budgets.

Workers receive separate `candidate/` seeds containing the original algorithm,
export wrapper, task instructions and a stdlib `research.py` helper. The helper
supports immutable submissions, polling, captured knowledge snapshots, reads
and sourced/CAS notes or skills. It excludes installed dependencies, Git data
and Python caches from uploads; symlinks are rejected. Retry the same operation
ID with the same bytes after transport failure. A changed proposal needs a new ID.

The recipe declares reflect every 5 local results, consolidate every 10 global
results, and pivot after a 5-result plateau, with `reference_manager` lifecycle.
These are explicit experimental settings and adapted prompts, not a claim of
the paper's exact default configuration. Configuration can replace the worker
runtime, cadence, prompt, task or final-selection policy through existing
interfaces. The domain's evidence stage can summarize released evaluations;
the persistent worker consumes no placeholder echo-actor calls.

Resume uses the same manifest, lock and output directory:

```sh
proteus recipe resume packing-two/recipe.json \
  --plugin proteus.benchmarks.coral_circle_packing.recipe:register \
  --lock packing-two/lock.json --out packing-two/run
```

Numeric conformance tests execute the pinned upstream grader in a separate
Python process. The live Docker gate compares original, valid-grid and invalid
forged candidates, including confirmation trials and no-repeat recovery. Model
quality, single/multi-worker tradeoffs and full R05 acceptance remain unfinished.
