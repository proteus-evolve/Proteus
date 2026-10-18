"""Installed evaluator scheduling, private receipts and exported accounting."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile

from proteus.core.contracts import Usage
from proteus.core.goal import EvalResult, EvaluatorSpec, GoalConfig, GoalContext
from proteus.core.native_inputs import read_native_inputs
from proteus.recipes import load_recipe
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.config import PluginSpec
from proteus.recipes.export import export_run, report_run
from proteus.recipes.manifest import SourcePin
from proteus.recipes.runner import run_recipe


def check():
    with tempfile.TemporaryDirectory(prefix='proteus-native-evaluator-package-') as directory:
        root = Path(directory)/'run'
        recipe = load_recipe(Path(__file__).with_name('native.toml'))
        recipe = replace(recipe, method=PluginSpec(plugin=recipe.method.plugin, options={
            **dict(recipe.method.options), 'evaluators': ['tool-calls'], 'eval_at': ['tool-calls=0,2']}))
        registry = builtin_registry()
        lock = registry.make_lock(recipe, [SourcePin(name='proteus', repository='https://example.invalid/fixture', revision='0'*40)])
        original = None
        for resume in (False, True):
            result = run_recipe(recipe, registry=registry, lock=lock, root=root, resume=resume)
            assert result.status.value == 'completed', result.diagnostic
            rows = read_native_inputs(root/'workspace')
            assert len(rows) == 10
            evaluations = [r for r in rows if r['input']['scope'] == 'native_evaluator']
            assert [r['input']['episode'] for r in evaluations] == [0, 2]
            if resume:
                assert rows == original
            else:
                original = rows
        snapshot = json.loads((root/'execution-0002.json').read_text())
        assert snapshot['native_inputs'] == rows
        report = report_run(root)
        assert report['native_accounting']['boundary_count'] == 10
        assert report['native_accounting']['reported']['model_calls'] == 0
        assert not any(path.startswith('.proteus-records/') for path in report['evidence'])
        assert sum(path.startswith('.proteus-records/') for path in report_run(root, include_private=True)['evidence']) == 20
        export_run(root, Path(directory)/'export')
        assert len((Path(directory)/'export/native-usage.csv').read_text().splitlines()) == 11
        def custom(trace, ctx):
            ctx.report_usage(Usage(model_calls=1, input_tokens=9, output_tokens=2))
            return EvalResult('custom', 1)
        other = Path(directory)/'custom'
        result = GoalConfig.of(evaluators=(EvaluatorSpec('custom', custom),)).evaluate([], GoalContext(
            'unused', 1, native_root=str(other)))
        assert result[0].status == 'ok'
        usage = read_native_inputs(other)[0]['outcome']['usage']['reported']
        assert usage['model_calls'] == 1 and usage['input_tokens'] == 9 and usage['cost_usd'] is None
        print('installed native evaluators: scheduled H0/episode receipts, explicit usage, private export and unchanged resume passed')


if __name__ == '__main__':
    check()
