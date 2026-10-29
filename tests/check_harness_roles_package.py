"""Exercise installed common Native roles from a prepared, zero-model-call recipe."""

import json
from pathlib import Path
import runpy
import tempfile

from proteus.api.v1 import SourcePin, builtin_registry, load_recipe, run_recipe
from proteus.examples.harness_roles import register
from proteus.recipes.export import report_run


def check():
    prepare = runpy.run_path(str(Path(__file__).with_name('prepare_harness_roles.py')))['prepare']
    with tempfile.TemporaryDirectory(prefix='proteus-installed-harness-roles-') as directory:
        root = Path(directory)
        path = prepare(root / 'study')
        registry = builtin_registry()
        register(registry)
        recipe = load_recipe(path)
        lock = registry.make_lock(recipe, [SourcePin(name='proteus',
            repository='https://example.invalid/installed-offline-fixture', revision='0'*40)])
        result = run_recipe(recipe, registry=registry, lock=lock, root=root / 'run',
            artifact_source=path.parent / 'artifact-store')
        assert result.status.value == 'completed', result.diagnostic
        records = json.loads((root / 'run/execution-0001.json').read_text())
        assert [row['candidate_id'] for row in records['rollouts']] == [
            'initial', 'episode-2', 'episode-3', 'episode-3']
        assert len(records['actor_calls']) == 3
        public, owner = report_run(root / 'run'), report_run(root / 'run', include_private=True)
        assert {(row['candidate_id'], row['purpose']): row['score'] for row in owner['tasks']} == {
            ('initial', 'optimization'): 0, ('episode-2', 'optimization'): 1,
            ('episode-3', 'optimization'): 1, ('episode-3', 'test'): 1}
        assert owner['accounting']['actual']['model_calls'] == 0
        assert owner['accounting']['actual']['evaluations'] == 4
        assert next(row['score'] for row in public['tasks'] if row['visibility'] == 'hidden') is None
        assert run_recipe(recipe, registry=registry, lock=lock, root=root / 'run', resume=True) == result
        resumed = json.loads((root / 'run/execution-0002.json').read_text())
        assert resumed['actor_calls'] == records['actor_calls']
        print(json.dumps({'installed_harness_roles': 'passed', 'task_evaluations': 4,
                          'model_calls': 0, 'episodes': 3, 'completed_resume_dispatches': 0}))


if __name__ == '__main__':
    check()
