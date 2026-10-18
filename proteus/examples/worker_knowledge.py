"""Two real workers exchange a useful note and publish a sourced skill."""

import argparse
from dataclasses import replace
import hashlib
from pathlib import Path
import sys

from proteus.examples.candidate_evaluation import plugin
from proteus.examples.evaluation_gateway import prepare as prepare_gateway, register as register
from proteus.recipes import load_recipe
from proteus.workers.records import CommandWorkerOptions


def prepare(root):
    path = prepare_gateway(root)
    recipe = load_recipe(path)
    command = (sys.executable, str(Path(__file__).with_name('_knowledge_worker_fixture.py').resolve()))
    options = CommandWorkerOptions(command=command,
        command_files={name: 'sha256:'+hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in command},
        timeout_seconds=60, shutdown_grace_seconds=.2)
    recipe = replace(recipe, name='offline-worker-knowledge',
        workers=tuple(replace(worker, runtime=plugin('proteus.worker.command', **options.to_dict())) for worker in recipe.workers),
        gateways=(replace(recipe.gateways[0], knowledge_access='read_write'),))
    path.write_text(recipe.to_json()+'\n')
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    print(prepare(parser.parse_args().out))


if __name__ == '__main__':
    main()
