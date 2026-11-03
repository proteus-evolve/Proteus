"""Installed package acceptance for an actual worker-to-worker knowledge exchange."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile

from proteus.core.ledger import Ledger
from proteus.examples.worker_knowledge import prepare, register
from proteus.knowledge.records import KnowledgeRevision
from proteus.recipes import load_recipe
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.manifest import RunManifest, SourcePin


def check():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        path = prepare(root/'study')
        registry = builtin_registry()
        register(registry)
        recipe = registry.resolve(load_recipe(path))
        lock = registry.make_lock(recipe, [SourcePin(name='proteus', repository='https://example.invalid/package', revision='0'*40)])
        (root/'lock.json').write_text(lock.to_json())
        args = [str(path), '--plugin', 'proteus.examples.worker_knowledge:register', '--lock', str(root/'lock.json'),
                '--out', str(root/'run')]
        cli = [sys.executable, '-m', 'proteus.cli', 'recipe']
        subprocess.run([*cli, 'run', *args, '--artifact-store', str(root/'study/artifact-store')],
                       check=True, capture_output=True, text=True, timeout=90)
        run = root/'run'
        manifest = RunManifest.from_json((run/'manifest.json').read_text())
        ledger = Ledger(run/'private/ledger.sqlite3', run_id=manifest.run_id,
                        config_hash=manifest.config_hash, lock_hash=manifest.lock_hash)
        publications = [json.loads(path.read_text()) for path in (run/'workers').glob('*/workspace/publication.json')]
        revisions = [KnowledgeRevision.from_dict(item['record']['revision']) for item in publications]
        note = next(item for item in revisions if item.write.kind == 'notes')
        skill = next(item for item in revisions if item.write.kind == 'skills')
        assert note.write.writer_id == 'a' and skill.write.writer_id == 'b' and note.ref() in skill.write.sources
        assert len(skill.write.sources) == 2
        strategy, = (run/'workers').glob('*/workspace/strategy.json')
        assert json.loads(strategy.read_text())['record']['ref'] == note.ref().to_dict()
        records = json.loads((run/'execution-0001.json').read_text())
        assert len(records['rollouts']) == 5
        assert sorted([row['score'] for row in batch['rows']] for batch in records['evaluations']) == [[0, 0], [1, 1]]
        assert ledger.balance()['active_slots'] == 0
        before = ledger.events()
        subprocess.run([*cli, 'resume', *args], check=True, capture_output=True, text=True, timeout=90)
        assert ledger.events() == before
        subprocess.run([sys.executable, '-m', 'proteus.knowledge.client', '--help'], check=True, capture_output=True)
        print('Installed worker knowledge: shared note changed a real worker candidate; formal attempt and note source preserved in its skill; CLI resume repeated no evaluations.')


if __name__ == '__main__':
    check()
