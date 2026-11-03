"""Run from outside the checkout after installing either wheel or sdist."""

import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile

from proteus.benchmarks.coral_circle_packing import ROOT, reference
from proteus.benchmarks.coral_circle_packing.recipe import prepare, register
from proteus.benchmarks.coral_circle_packing.verify import grade
from proteus.core.serialization import content_hash
from proteus.examples.candidate_evaluation import plugin
from proteus.recipes import load_recipe
from proteus.recipes.builtins import builtin_registry
from proteus.workers.records import CommandWorkerOptions

assert len(reference()['files']) == 4
assert all((ROOT/name).is_file() for name in ('candidate.py', 'verify.py', 'research.py', 'NOTICE.md'))
assert grade({'centers': [[.5, .5]]*26, 'radii': [0]*26})['score'] == 0
options = CommandWorkerOptions(command=(sys.executable,), command_files={sys.executable:
    'sha256:'+hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest()})
with tempfile.TemporaryDirectory(prefix='packing-package-') as directory:
    for workers in (0, 1, 2):
        path = prepare(Path(directory)/str(workers), '/pinned/harbor/python', workers=max(1, workers),
            worker_runtime=plugin('proteus.worker.command', **options.to_dict()) if workers else None,
            runtime={'runtime_hash': content_hash('fixture')})
        registry = builtin_registry()
        register(registry)
        recipe = registry.resolve(load_recipe(path))
        assert len(recipe.workers) == workers
    subprocess.run([sys.executable, '-m', 'proteus.benchmarks.coral_circle_packing.recipe', '--help'], check=True,
                   stdout=subprocess.DEVNULL)
    subprocess.run([sys.executable, '-I', str(ROOT/'research.py'), '--help'], check=True, stdout=subprocess.DEVNULL)
print('Installed circle packing: pinned reference, private verifier, standalone research CLI and baseline/one/two-worker recipes passed.')
