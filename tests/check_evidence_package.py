"""Exercise installed history disclosure and resume without pytest or model services."""

import json
import tempfile
from pathlib import Path

from proteus.core.methods import MethodResult
from proteus.evidence.store import EvidenceStore
from proteus.examples.evidence_history import prepare, register
from proteus.recipes import load_recipe
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.manifest import SourcePin
from proteus.recipes.runner import run_recipe


def check():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        recipe = load_recipe(prepare(root / 'study'))
        registry = builtin_registry()
        register(registry)
        recipe = registry.resolve(recipe)
        lock = registry.make_lock(recipe, [SourcePin(name='proteus', repository='https://example.invalid/offline-fixture',
                                                    revision='0' * 40)])
        result = run_recipe(recipe, registry=registry, lock=lock, root=root / 'run',
                            artifact_source=root / 'study/artifact-store')
        assert result.status.value == 'completed', result.diagnostic
        evidence = EvidenceStore(root / 'run/evidence')
        ref = result.outputs['history']
        view = evidence.verify(ref)
        assert view.candidate_ids == ('initial', 'doubled')
        assert {entry.evaluation_id for entry in view.entries} == {None, 'validation'}
        assert len([entry for entry in view.entries if entry.kind == 'rollout']) == 4
        evidence.materialize(ref, root / 'view')
        evidence.verify_materialized(ref, root / 'view')
        records = json.loads((root / 'run/execution-0001.json').read_text())
        assert len(records['rollouts']) == 6
        resumed = run_recipe(recipe, registry=registry, lock=lock, root=root / 'run', resume=True)
        assert resumed == result == MethodResult.from_json((root / 'run/result.json').read_text())
        assert len(json.loads((root / 'run/execution-0002.json').read_text())['rollouts']) == 6
        assert evidence.verify(ref) == view
    print('Installed evidence history: two candidate codes, observed validation only, immutable export and resume passed.')


if __name__ == '__main__':
    check()
