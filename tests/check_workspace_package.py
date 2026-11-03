"""Exercise file proposals from an installed distribution, outside the checkout."""

import json
from pathlib import Path
import tempfile

from proteus.examples.coding_workspace import prepare, register
from proteus.recipes import load_recipe
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.manifest import SourcePin
from proteus.recipes.runner import run_recipe


def check():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        recipe = load_recipe(prepare(root/'study'))
        registry = builtin_registry()
        register(registry)
        lock = registry.make_lock(recipe,[SourcePin(name='proteus',repository='https://example.invalid/workspace',revision='0'*40)])
        result = run_recipe(recipe,registry=registry,lock=lock,root=root/'run',artifact_source=root/'study/artifact-store')
        assert result.status.value == 'completed',result.diagnostic
        records = json.loads((root/'run/execution-0001.json').read_text())
        assert len(records['candidates']) == 5 and len(records['actor_calls']) == 1
        assert [r['output'] for r in records['rollouts'][-6:]] == [4,6,6,9,8,12]
        assert records['actor_calls'][0]['result']['workspace_artifact']
        assert run_recipe(recipe,registry=registry,lock=lock,root=root/'run',resume=True) == result
    print('Installed file proposer: two historical candidates, three new artifacts, twelve predictions and no-repeat resume passed.')


if __name__ == '__main__':
    check()
