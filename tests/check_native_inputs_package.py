"""Exercise packaged native recipe evidence, budget rendering and no-work resume."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile

from proteus.core.native_inputs import input_directory, read_native_inputs
from proteus.recipes import load_recipe
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.config import PluginSpec
from proteus.recipes.manifest import SourcePin
from proteus.recipes.runner import run_recipe
from proteus.recipes.export import export_run, report_run


def check():
    with tempfile.TemporaryDirectory(prefix='proteus-native-input-package-') as directory:
        root=Path(directory)/'run'
        # Installed built-in example, copied beside this script by the caller.
        recipe=load_recipe(Path(__file__).with_name('native.toml'))
        recipe=replace(recipe, method=PluginSpec(plugin=recipe.method.plugin,
            options={**dict(recipe.method.options),'announce_budget':True}))
        registry=builtin_registry()
        lock=registry.make_lock(recipe,[SourcePin(name='proteus',repository='https://example.invalid/fixture',revision='0'*40)])
        result=run_recipe(recipe,registry=registry,lock=lock,root=root)
        assert result.status.value=='completed',result.diagnostic
        first=json.loads((root/'execution-0001.json').read_text())
        records=read_native_inputs(root/'workspace')
        assert len(records)==8 and records==first['native_inputs']
        assert all(row['completion']=='returned' for row in records)
        assert all(row['input']['payload']!=first['native_prompts'][row['input']['episode']-1]['prompts'][row['input']['phase']]
                   for row in records)
        before={path.name:path.read_bytes() for path in input_directory(root/'workspace').glob('*.json')}
        resumed=run_recipe(recipe,registry=registry,lock=lock,root=root,resume=True)
        assert resumed.status.value=='completed'
        assert json.loads((root/'execution-0002.json').read_text())['native_inputs']==records
        assert before=={path.name:path.read_bytes() for path in input_directory(root/'workspace').glob('*.json')}
        usage=report_run(root)['native_accounting']
        assert usage['boundary_count']==8 and usage['reported']['model_calls']==0
        assert usage['reported']['input_tokens']==usage['reported']['output_tokens']==0
        assert usage['reported']['cost_usd']==0 and usage['reported']['wall_seconds']>=0
        assert usage['unknown_entries']['model_calls']==0 and not usage['budget_enforced']
        export_run(root,Path(directory)/'export')
        assert len((Path(directory)/'export/native-usage.csv').read_text().splitlines())==9
        assert before=={path.name:path.read_bytes() for path in input_directory(root/'workspace').glob('*.json')}
        print('installed native recipe: eight immutable inputs/usage receipts, zero mock inference, unchanged resume and CSV export passed')


if __name__=='__main__':
    check()
