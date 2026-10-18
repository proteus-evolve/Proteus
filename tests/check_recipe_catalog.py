"""Run installed catalog examples with explicit fixture source provenance."""

import argparse
import json
from pathlib import Path
import tempfile

from proteus.core.ledger import Ledger
from proteus.core.serialization import content_hash
from proteus.recipes import load_recipe
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.catalog import prepare_example
from proteus.recipes.manifest import SourcePin
from proteus.recipes.runner import run_recipe


def check_example(root, name):
    root = Path(root)
    prepared = prepare_example(name, root/'study')
    recipe = load_recipe(prepared['recipe'])
    assert recipe.config_hash == prepared['config_hash']
    registry = builtin_registry()
    for hook in prepared['catalog_entry']['plugins']:
        registry.load(hook)
    lock = registry.make_lock(recipe, [SourcePin(name='proteus', repository='https://example.invalid/catalog-fixture',
                                                revision='0'*40)])
    options = {'artifact_source': root/'study/artifact-store'} if recipe.subject else {}
    result = run_recipe(recipe, registry=registry, lock=lock, root=root/'run', **options)
    assert result.status.value == 'completed', result.diagnostic
    ledger = Ledger(root/'run/private/ledger.sqlite3', run_id='run', config_hash=recipe.config_hash,
                    lock_hash=content_hash(lock))
    before = ledger.events()
    records = json.loads((root/'run/execution-0001.json').read_text())
    assert records['candidates'] and records['rollouts']
    assert ledger.balance()['active_slots'] == 0
    assert run_recipe(recipe, registry=registry, lock=lock, root=root/'run', resume=True) == result
    assert ledger.events() == before
    resumed = json.loads((root/'run/execution-0002.json').read_text())
    for key in ('actor_calls', 'candidates', 'rollouts', 'evaluations'):
        assert resumed[key] == records[key], (name, key)
    return records


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--gepa', action='store_true', help='run the two examples requiring the pinned optional GEPA')
    args = parser.parse_args()
    names = ('gepa', 'combee-gepa') if args.gepa else (
        'candidate-comparison', 'search-policy', 'ace', 'meta-harness', 'combee-ace', 'coral')
    with tempfile.TemporaryDirectory(prefix='proteus-catalog-package-') as directory:
        for name in names:
            records = check_example(Path(directory)/name, name)
            print(name, 'actual example passed:', len(records['candidates']), 'candidates,',
                  len(records['rollouts']), 'trials; no-repeat resume')
