"""Run the installed candidate-code protocol, memory update and resume without pytest."""

import json
import sys
import tempfile
from pathlib import Path

from proteus.core.contracts import EvaluationBatch
from proteus.examples.code_memory import prepare, register
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
        recipe = registry.resolve(recipe)
        lock = registry.make_lock(recipe, [SourcePin(name='proteus', repository='https://example.invalid/protocol-fixture',
                                                    revision='0'*40)])
        result = run_recipe(recipe, registry=registry, lock=lock, root=root/'run', artifact_source=root/'study/artifact-store')
        assert result.status.value == 'completed', result.diagnostic
        records = json.loads((root/'run/execution-0001.json').read_text())
        assert [EvaluationBatch.from_dict(item).mean_score() for item in records['evaluations']] == [0,1]
        assert len(records['actor_calls']) == 7 and len(records['rollouts']) == 5 and len(records['candidates']) == 1
        assert sum(call['usage']['model_calls'] for call in records['actor_calls']) == 0  # echo fixture only
        memory = next(item['value'] for item in records['values'] if item['ref'] == result.outputs['memory'].to_dict())
        assert memory['value'] == {'factor':2}
        resumed = run_recipe(recipe, registry=registry, lock=lock, root=root/'run', resume=True)
        assert resumed == result
        assert len(json.loads((root/'run/execution-0002.json').read_text())['actor_calls']) == 7
        from proteus.examples.harbor_code_memory import prepare as prepare_harbor
        from proteus.execution.harbor_code import WORKER
        from proteus.core.serialization import content_hash
        container_recipe = load_recipe(prepare_harbor(root/'harbor-study', sys.executable,
            runtime={'runtime_hash': content_hash('package-inspection-only')}))
        assert registry.resolve(container_recipe).execution.environment.network == 'none'
        assert WORKER.is_file()  # The optional worker is present in both installed distributions.
    print('Installed code protocol: five evaluations, separate memory update, seven managed echo calls and no-repeat resume passed.')


if __name__ == '__main__':
    check()
