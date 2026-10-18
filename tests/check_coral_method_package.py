"""Installed CORAL controller acceptance through the public CLI outside the checkout."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile

from proteus.core.ledger import Ledger
from proteus.examples.coral_search import prepare, register
from proteus.recipes import load_recipe
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.manifest import RunManifest, SourcePin


def check():
    for workers in (1, 2):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = prepare(root/'study', workers=workers)
            registry = builtin_registry()
            register(registry)
            recipe = registry.resolve(load_recipe(path))
            lock = registry.make_lock(recipe, [SourcePin(name='proteus', repository='https://example.invalid/package',
                                                       revision='0'*40)])
            (root/'lock.json').write_text(lock.to_json())
            args = [str(path), '--plugin', 'proteus.examples.coral_search:register',
                    '--lock', str(root/'lock.json'), '--out', str(root/'run')]
            cli = [sys.executable, '-m', 'proteus.cli', 'recipe']
            subprocess.run([*cli, 'run', *args, '--artifact-store', str(root/'study/artifact-store')],
                check=True, capture_output=True, text=True, timeout=90)
            run = root/'run'
            manifest = RunManifest.from_json((run/'manifest.json').read_text())
            ledger = Ledger(run/'private/ledger.sqlite3', run_id=manifest.run_id,
                config_hash=manifest.config_hash, lock_hash=manifest.lock_hash)
            execution = json.loads((run/'execution-0001.json').read_text())
            summary = next(item['value'] for item in execution['values'] if item['ref']['kind'] == 'coral.summary@1')
            assert summary['reason'] == 'submission_limit' and len(summary['results']) == workers + 1
            assert len(summary['final_evaluations']) == 1
            assert len(execution['rollouts']) == 2 + 2*(workers + 1) + 1
            assert len([item for item in execution['values'] if item['ref']['kind'] == 'proteus.event.dispatch@1']) == workers
            launches = [json.loads(line) for path in (run/'workers').glob('*/workspace/launches.jsonl')
                        for line in path.read_text().splitlines()]
            assert len(launches) == 2*workers and sum(item['continuity'] == 'native' for item in launches) == workers
            assert ledger.balance()['active_slots'] == 0
            before = ledger.events()
            subprocess.run([*cli, 'resume', *args], check=True, capture_output=True, text=True, timeout=90)
            assert ledger.events() == before
    print('Installed CORAL method: one/two workers, shared knowledge, native interventions, final selection, '
          'hidden test, bounded shutdown and no-repeat CLI resume verified.')


if __name__ == '__main__':
    check()
