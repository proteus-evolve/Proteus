"""Prepare the code-memory fixture in offline Harbor containers (no model API key)."""

import argparse
from dataclasses import replace
from pathlib import Path

from proteus.examples.candidate_evaluation import plugin
from proteus.examples.code_memory import prepare as prepare_local
from proteus.examples.harbor_evaluation import IMAGE
from proteus.execution.harbor import identify_runtime
from proteus.recipes import load_recipe
from proteus.recipes.config import EnvironmentSpec


def prepare(root, python, *, runtime=None):
    path = prepare_local(root)
    recipe = load_recipe(path)
    identity = runtime or identify_runtime(python)
    environment = EnvironmentSpec(backend=plugin('proteus.harbor_code', python=python,
        runtime_hash=identity['runtime_hash']), provider='docker', image_digest=IMAGE,
        network='none', resources={'cpus': 1, 'memory_mb': 512})
    recipe = replace(recipe, name='offline-harbor-code-memory-fixture',
        execution=replace(recipe.execution, environment=environment, timeout_seconds=180))
    path.write_text(recipe.to_json() + '\n')
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--harbor-python', required=True)
    args = parser.parse_args()
    print(prepare(args.out, args.harbor_python))


if __name__ == '__main__':
    main()
