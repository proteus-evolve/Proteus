"""Installed public CLI acceptance for worker-submitted formal evaluations."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile

from proteus.artifacts.codecs import FileWrite
from proteus.core.ledger import Ledger
from proteus.examples.evaluation_gateway import prepare, register
from proteus.recipes import load_recipe
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.manifest import RunManifest, SourcePin


def check():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        path = prepare(root/'study')
        registry = builtin_registry()
        register(registry)
        recipe = registry.resolve(load_recipe(path))
        lock = registry.make_lock(recipe, [SourcePin(name='proteus', repository='https://example.invalid/package',
                                                    revision='0'*40)])
        (root/'lock.json').write_text(lock.to_json())
        args = [str(path), '--plugin', 'proteus.examples.evaluation_gateway:register', '--lock', str(root/'lock.json'),
                '--out', str(root/'run')]
        cli = [sys.executable, '-m', 'proteus.cli', 'recipe']
        subprocess.run([*cli, 'run', *args, '--artifact-store', str(root/'study/artifact-store')],
                       check=True, capture_output=True, text=True, timeout=90)
        run = root/'run'
        manifest = RunManifest.from_json((run/'manifest.json').read_text())
        ledger = Ledger(run/'private/ledger.sqlite3', run_id=manifest.run_id,
                        config_hash=recipe.config_hash, lock_hash=manifest.lock_hash)
        responses = [json.loads(path.read_text()) for path in (run/'workers').glob('*/workspace/response.json')]
        assert len(responses) == 2
        assert {response['ticket']['worker_id'] for response in responses} == {'a', 'b'}
        for response in responses:
            assert response['status'] == 'completed' and response['result']['shared_attempt']
            assert {entry['evaluation_id'] for entry in response['view']['entries']} == {None, 'validation'}
            assert b'later workspace edit' not in b'\n'.join(FileWrite.from_dict(row).content().data for row in response['files'])
        records = json.loads((run/'execution-0001.json').read_text())
        assert len(records['rollouts']) == 5
        assert sorted([row['score'] for row in batch['rows']] for batch in records['evaluations']) == [[0, 0], [1, 1]]
        assert ledger.balance()['charged']['evaluations'] == 5 and ledger.balance()['active_slots'] == 0
        before = ledger.events()
        subprocess.run([*cli, 'resume', *args], check=True, capture_output=True, text=True, timeout=90)
        assert ledger.events() == before
        subprocess.run([sys.executable, '-m', 'proteus.gateway.client', '--help'], check=True, capture_output=True)
        print('Installed gateway: two actual workers, frozen candidates, five predictions, released feedback, shared attempts and no-repeat CLI resume passed.')


if __name__ == '__main__':
    check()
