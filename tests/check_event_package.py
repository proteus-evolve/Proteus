"""Installed package acceptance: CLI, native interventions, reference resources and replay."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile

from proteus.core.ledger import Ledger
from proteus.examples.event_interventions import prepare, register
from proteus.methods import coral
from proteus.methods.coral.policy import register as register_coral
from proteus.recipes import load_recipe
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.manifest import RunManifest, SourcePin


def check():
    package = Path(coral.__file__).parent
    assert (package/'LICENSE-CORAL.txt').is_file() and (package/'NOTICE.md').is_file()
    assert all((package/f'{name}.reference.md').is_file() for name in ('reflect', 'consolidate', 'pivot'))
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        path = prepare(root/'study')
        registry = builtin_registry()
        register(registry)
        register_coral(registry)
        recipe = registry.resolve(load_recipe(path))
        lock = registry.make_lock(recipe, [SourcePin(name='proteus', repository='https://example.invalid/package', revision='0'*40)])
        (root/'lock.json').write_text(lock.to_json())
        args = [str(path), '--plugin', 'proteus.examples.event_interventions:register',
                '--plugin', 'proteus.methods.coral.policy:register', '--lock', str(root/'lock.json'), '--out', str(root/'run')]
        cli = [sys.executable, '-m', 'proteus.cli', 'recipe']
        subprocess.run([*cli, 'run', *args, '--artifact-store', str(root/'study/artifact-store')],
                       check=True, capture_output=True, text=True, timeout=90)
        run = root/'run'
        manifest = RunManifest.from_json((run/'manifest.json').read_text())
        ledger = Ledger(run/'private/ledger.sqlite3', run_id=manifest.run_id,
                        config_hash=manifest.config_hash, lock_hash=manifest.lock_hash)
        records = json.loads((run/'execution-0001.json').read_text())
        decisions = [item['value'] for item in records['values'] if item['ref']['kind'] == 'proteus.event.decision@1']
        assert sorted(item['observation']['score'] for item in decisions) == [0, 1]
        assert len([item for item in records['values'] if item['ref']['kind'] == 'proteus.event.dispatch@1']) == 2
        launches = [json.loads(line) for path in (run/'workers').glob('*/workspace/launches.jsonl')
                    for line in path.read_text().splitlines()]
        assert len(launches) == 4 and sum(item['continuity'] == 'native' for item in launches) == 2
        assert ledger.balance()['active_slots'] == 0
        before = ledger.events()
        subprocess.run([*cli, 'resume', *args], check=True, capture_output=True, text=True, timeout=90)
        assert ledger.events() == before
        print('Installed event policies: two formal scores, native worker interventions, immutable decisions, no-repeat CLI resume and packaged CORAL reference resources verified.')


if __name__ == '__main__':
    check()
