"""Exercise all generated plugins from an installed wheel/sdist or source tree."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile


CHECK = '''import importlib,json,sys
from dataclasses import replace
from pathlib import Path
from proteus.artifacts.codecs import FileContent
from proteus.artifacts.store import ArtifactStore
from proteus.core.contracts import DataRef,PluginRef
from proteus.core.ledger import Ledger
from proteus.core.serialization import ContractError,content_hash
from proteus.recipes import load_recipe
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.config import PluginSpec
from proteus.recipes.manifest import SourcePin
from proteus.recipes.runner import run_recipe

kind=sys.argv[1]; name='sample_'+kind
module=importlib.import_module(name)
registry=builtin_registry();module.register(registry)
recipe=load_recipe('study/recipe.json')
# These are explicit smoke-test fixture pins, not a real research source claim.
pins=[SourcePin(name=source,repository='https://example.invalid/template-fixture',revision='0'*40)
      for source in ('proteus',name)]
lock=registry.make_lock(recipe,pins)
Path('fixture-lock.json').write_text(lock.to_json())
options={'artifact_source':Path('study/artifact-store')} if recipe.subject else {}
result=run_recipe(recipe,registry=registry,lock=lock,root='run',**options)
assert result.status.value=='completed',result.diagnostic
record=json.loads(Path('run/execution-0001.json').read_text())
ledger=Ledger(Path('run/private/ledger.sqlite3'),run_id='run',config_hash=recipe.config_hash,lock_hash=content_hash(lock))
events=ledger.events()
assert ledger.balance()['active_slots']==0
if kind=='stage':
    assessment=next(row['value'] for row in record['values'] if row['ref']['kind']==name+'.assessment@1')
    assert assessment['meets_length'] and assessment['characters']==20
    assert len(record['actor_calls'])==1 and not record['evaluations']
else:
    expected=[0.0,1.0] if kind=='evaluator' else ([1.0] if kind=='subject' else [0.0])
    assert [batch['rows'][0]['score'] for batch in record['evaluations']]==expected
    assert ledger.balance()['charged']['evaluations']==(6 if kind=='evaluator' else 3)
    # Every hidden-test output is still a score-free receipt.
    for output,ref in result.outputs.items():
        if output.endswith('test'):
            value=next(row['value'] for row in record['values'] if row['ref']==ref.to_dict())
            assert value['batches']==[]
assert run_recipe(recipe,registry=registry,lock=lock,root='run',resume=True)==result
assert ledger.events()==events
again=json.loads(Path('run/execution-0002.json').read_text())
for key in ('candidates','rollouts','evaluations','actor_calls'):
    assert record[key]==again[key]
if kind=='stage':
    changed=replace(recipe,stages=(recipe.stages[0],replace(recipe.stages[1],operator=PluginSpec(
        plugin=recipe.stages[1].operator.plugin,options={'minimum_characters':1000}))))
    changed=registry.resolve(changed)
    output=run_recipe(changed,registry=registry,lock=registry.make_lock(changed,pins),root='ablation')
    assert output.status.value=='completed',output.diagnostic
    entries=json.loads(Path('ablation/execution-0001.json').read_text())['values']
    assert not next(row['value']['meets_length'] for row in entries if row['ref']['kind']==name+'.assessment@1')
if kind=='subject':
    store=ArtifactStore('study/artifact-store',registry)
    artifact=store.put(PluginRef(name='proteus.file_tree',version='1'),{'factor.json':FileContent(b'{"factor":3}')})
    changed=registry.resolve(replace(recipe,subject=replace(recipe.subject,initial_artifact=artifact)))
    output=run_recipe(changed,registry=registry,lock=registry.make_lock(changed,pins),root='ablation',**options)
    assert output.status.value=='completed',output.diagnostic
    records=json.loads(Path('ablation/execution-0001.json').read_text())
    assert [row['output'] for row in records['rollouts']]==[6,9,21]
    assert records['evaluations'][0]['rows'][0]['score']==0
path=Path(name+'.py');path.write_text(path.read_text()+'\\n# changed implementation\\n')
changed_registry=builtin_registry();module.register(changed_registry)
try:
    changed_registry.check_lock(recipe,lock)
except ContractError:
    pass
else:
    raise AssertionError('changed implementation admitted by an old lock')
print(kind+': plugin run, semantic assertions, private test receipt, call-free resume and code lock passed')
'''


def check_project(root, kind):
    project = Path(root)/kind
    environment = dict(os.environ)
    pythonpath = environment.get('PYTHONPATH')
    if pythonpath:
        environment['PYTHONPATH'] = os.pathsep.join(str(Path(item).resolve()) for item in pythonpath.split(os.pathsep) if item)

    def run(arguments, cwd=None):
        result = subprocess.run([sys.executable, *arguments], cwd=cwd, env=environment,
                                capture_output=True, text=True, timeout=90)
        assert result.returncode == 0, result.stdout+'\n'+result.stderr
        return result.stdout

    run(['-m', 'proteus.cli', 'recipe', 'scaffold', kind, '--name', 'sample_'+kind, '--out', str(project)])
    run(['prepare.py'], project)
    run(['-m', 'proteus.cli', 'recipe', 'check', 'study/recipe.json', '--plugin', 'sample_'+kind+':register'], project)
    checked = run(['check.py'], project)
    if kind == 'policy':
        return checked
    (project/'verify_project.py').write_text(CHECK)
    return run(['verify_project.py', kind], project)


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='proteus-plugin-projects-') as directory:
        for kind in ('method', 'stage', 'evaluator', 'subject', 'policy'):
            print(check_project(directory, kind), end='')
