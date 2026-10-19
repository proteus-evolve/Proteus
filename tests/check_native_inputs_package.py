"""Exercise packaged native recipe evidence, budget rendering and no-work resume."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile

from proteus.core.native_inputs import input_directory, read_native_inputs
from proteus.core.native_inputs import native_input
from proteus.core.native_usage import codex_usage, summarize_native_inputs
from proteus.core.adapter import EpisodeSpec
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
        assert len(records)==10 and records==first['native_inputs']
        assert all(row['completion']=='returned' for row in records)
        assert all(row['input']['payload']!=first['native_prompts'][row['input']['episode']-1]['prompts'][row['input']['phase']]
                   for row in records if row['input']['scope'] == 'policy_prompt')
        before={path.name:path.read_bytes() for path in input_directory(root/'workspace').glob('*.json')}
        resumed=run_recipe(recipe,registry=registry,lock=lock,root=root,resume=True)
        assert resumed.status.value=='completed'
        assert json.loads((root/'execution-0002.json').read_text())['native_inputs']==records
        assert before=={path.name:path.read_bytes() for path in input_directory(root/'workspace').glob('*.json')}
        usage=report_run(root)['native_accounting']
        assert usage['boundary_count']==10 and usage['reported']['model_calls']==0
        assert usage['reported']['input_tokens']==usage['reported']['output_tokens']==0
        assert usage['reported']['cost_usd']==0 and usage['reported']['wall_seconds']>=0
        assert usage['unknown_entries']['model_calls']==0 and not usage['budget_enforced']
        export_run(root,Path(directory)/'export')
        assert len((Path(directory)/'export/native-usage.csv').read_text().splitlines())==11
        assert before=={path.name:path.read_bytes() for path in input_directory(root/'workspace').glob('*.json')}
        print('installed native recipe: ten immutable adapter/evaluator receipts, zero mock inference, unchanged resume and CSV export passed')
        native=Path(directory)/'codex'
        source=('\n'.join(json.dumps(event) for event in [
            {'type':'thread.started','thread_id':'12345678-1234-4234-8234-123456789abc'},
            {'type':'turn.started'},
            {'type':'turn.completed','usage':{'input_tokens':13,'cached_input_tokens':5,'output_tokens':7}},
        ])+'\n').encode()
        with native_input(EpisodeSpec(root=native,episode=1,model='fixture',phase_prompts={'act':'fixture'}),backend='codex',phase='act',scope='harness_cli',
                          payload=['exec','--json','--ephemeral','fixture']) as receipt:
            receipt['usage_source']=source
            receipt['usage']=codex_usage(source,transport_complete=True)
            receipt['returncode']=0
        totals=summarize_native_inputs(read_native_inputs(native))
        assert totals['reported']['input_tokens']==13 and totals['reported']['output_tokens']==7
        assert totals['reported']['model_calls'] is None and totals['reported']['cost_usd'] is None
        assert next(input_directory(native).glob('*.usage-source')).read_bytes()==source
        print('installed native Codex: immutable raw JSON receipt, strict parser and unknown call/cost totals passed')


if __name__=='__main__':
    check()
