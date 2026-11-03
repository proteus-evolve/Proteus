"""Editable worker seeds preserve exact initial bytes and never reset later work."""

from dataclasses import replace
import os
from pathlib import Path
import subprocess
import sys

import pytest

from proteus.artifacts.store import ArtifactStore
from proteus.core.serialization import ContractError, content_hash
from proteus.workers.records import WorkerLaunch
from proteus.workers.runtime import immutable, write_json
from proteus.workers.seeding import locations, validate_seed
from test_v1_gateway import setup, services


def seeded_services(root):
    recipe, registry, lock = setup(root)
    current = services(root, recipe, registry, lock)
    current.artifacts.copy_from(ArtifactStore(root/'study/artifact-store', registry), recipe.subject.initial_artifact)
    return current


def test_seed_is_exact_independent_and_preserves_edits_after_launch(tmp_path):
    current = seeded_services(tmp_path)
    artifact = current.recipe.subject.initial_artifact
    worker = current.worker('a')
    ref = worker.seed('initial', artifact)
    seed = current.values.get(ref)
    destination = worker.controller.workspace/'candidate'
    current.artifacts.verify_tree(artifact, destination)
    # A launch request is sufficient to prove that workspace edits are now owned
    # by a worker. The lifecycle/integration suites exercise actual processes.
    launch = WorkerLaunch(identity=worker.controller.identity, runtime=worker.controller.runtime_ref,
        options=worker.controller.options.to_dict(), operation_id='search', prompt='research',
        workspace=str(worker.controller.workspace))
    directory = worker.controller._directory('search')
    directory.mkdir()
    immutable(directory/'request.json', launch)
    write_json(worker.controller.private/'head.json', launch.ref())
    path = next(item for item in destination.rglob('*') if item.is_file())
    path.write_text('new worker research')
    assert worker.seed('initial', artifact) == ref
    validate_seed(current, seed)
    assert path.read_text() == 'new worker research'
    assert current.artifacts.files(artifact)[path.relative_to(destination).as_posix()].data != path.read_bytes()
    with pytest.raises(ContractError, match='identity changed'):
        worker.seed('different', artifact)
    with pytest.raises(ContractError, match='precede every launch'):
        worker.seed('later', artifact, directory='other')


def test_seed_rejects_existing_unowned_paths_and_changed_receipts(tmp_path):
    current = seeded_services(tmp_path)
    worker = current.worker('a')
    destination = worker.controller.workspace/'candidate'
    destination.mkdir()
    (destination/'keep.txt').write_text('user content')
    with pytest.raises(ContractError, match='without a saved intent'):
        worker.seed('initial', current.recipe.subject.initial_artifact)
    assert (destination/'keep.txt').read_text() == 'user content'
    ref = worker.seed('initial', current.recipe.subject.initial_artifact, directory='other')
    value = current.values.get(ref)
    with pytest.raises(ContractError, match='receipts'):
        validate_seed(current, replace(value, operation_id='changed'))
    with pytest.raises(ContractError):
        worker.seed('escape', current.recipe.subject.initial_artifact, directory='../outside')


SEED_CRASH = r'''
import os, sys
from pathlib import Path
from proteus.artifacts.store import ArtifactStore
from proteus.workers import seeding
from proteus.recipes import load_recipe
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.manifest import DependencyLock
from proteus.examples.evaluation_gateway import register
from test_v1_gateway import services
root,boundary=Path(sys.argv[1]),sys.argv[2]
recipe=load_recipe(root/'recipe.json'); registry=builtin_registry(); register(registry)
lock=DependencyLock.from_json((root/'lock.json').read_text())
current=services(root,recipe,registry,lock)
current.artifacts.copy_from(ArtifactStore(root/'study/artifact-store',registry),recipe.subject.initial_artifact)
materialize, write, rename=ArtifactStore.materialize,seeding._write,Path.rename
def materialize_crash(self,ref,destination):
    if boundary=='partial':
        destination.mkdir(); (destination/'partial').write_text('interrupted copy'); os._exit(86)
    return materialize(self,ref,destination)
def rename_crash(self,destination):
    result=rename(self,destination)
    if boundary=='renamed' and self.name.startswith('.proteus-seed-'): os._exit(86)
    return result
def write_crash(path,value):
    write(path,value)
    if boundary==path.name: os._exit(86)
ArtifactStore.materialize=materialize_crash; Path.rename=rename_crash; seeding._write=write_crash
current.worker('a').seed('initial',recipe.subject.initial_artifact)
'''


@pytest.mark.parametrize('boundary', ['request.json', 'partial', 'renamed', 'seed.json'])
def test_seed_hard_exit_recovers_only_the_bound_initial_copy(tmp_path, boundary):
    current = seeded_services(tmp_path)
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(Path(__file__).parent), str(Path(__file__).parents[1])]))
    result = subprocess.run([sys.executable, '-c', SEED_CRASH, str(tmp_path), boundary], env=env,
                            capture_output=True, timeout=15)
    assert result.returncode == 86, result.stderr.decode()
    worker = current.worker('a')
    ref = worker.seed('initial', current.recipe.subject.initial_artifact)
    value = current.values.get(ref)
    receipts, destination, staging = locations(worker.controller, 'candidate')
    assert (receipts/'seed.json').exists() and not staging.exists()
    validate_seed(current, value)
    current.artifacts.verify_tree(value.artifact, destination)
    assert value.identity.config_hash == current.recipe.config_hash
    assert content_hash(value.artifact) == content_hash(current.recipe.subject.initial_artifact)
