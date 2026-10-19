"""Run and summarize the installed offline multi-seed study outside the checkout."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile

from proteus.examples.search_policy import register
from proteus.examples.search_policy_study import prepare
from proteus.recipes import load_recipe
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.manifest import SourcePin
from proteus.recipes.runner import run_recipe


def check(root):
    root = Path(root)
    spec = prepare(root/'study')
    index = json.loads((spec.parent/'experiments.json').read_text())
    registry = builtin_registry()
    register(registry)
    for entry in index['experiments']:
        path = Path(entry['recipe'])
        recipe = load_recipe(path)
        assert entry['config_hash'] == recipe.config_hash
        lock = registry.make_lock(recipe, [SourcePin(name='proteus', repository='https://example.invalid/study-fixture', revision='0'*40)])
        result = run_recipe(recipe, registry=registry, lock=lock, root=path.parent/'run', artifact_source=path.parent/'artifact-store')
        assert result.status.value == 'completed', result.diagnostic
    output = root/'comparison.json'
    subprocess.run([sys.executable, '-m', 'proteus.cli', 'recipe', 'summarize', str(spec),
                    '--include-private', '--out', str(output)], check=True, capture_output=True)
    report = json.loads(output.read_text())
    assert len(report['rows']) == 4
    assert report['paired_deltas'][0]['quality_delta']['mean'] == -.4
    assert all(row['reference']['status'] == 'experimental' for row in report['rows'])
    print('Installed multi-seed study: four real runs, controlled comparison and paired deltas passed')


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='proteus-study-package-') as directory:
        check(directory)
