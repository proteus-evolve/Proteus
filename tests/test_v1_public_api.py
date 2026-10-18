"""External plugin execution through versioned imports, parent lineage and recovery."""

import os
from pathlib import Path
import subprocess
import sys

import pytest

from proteus.recipes.scaffolding import scaffold


@pytest.mark.parametrize('boundary', ['parent', 'child', 'test'])
def test_external_parent_policy_recovers_without_reselecting_or_recharging(tmp_path, boundary):
    project = scaffold('policy', 'outside_parent', tmp_path/'project')
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    subprocess.run([sys.executable, 'prepare.py'], cwd=project, env=env, check=True, capture_output=True)
    program = '''import os,sys
from pathlib import Path
from proteus.api.v1 import RunServices,SourcePin,builtin_registry,load_recipe,run_recipe
from proteus.recipes.export import report_run
from outside_parent import register
registry=builtin_registry();register(registry)
recipe=load_recipe('study/recipe.json')
lock=registry.make_lock(recipe,[SourcePin(name=name,repository='https://example.invalid/recovery-fixture',revision='0'*40)
 for name in ('proteus','outside_parent')])
if sys.argv[1]=='stop':
 original=RunServices.checkpoint
 def stop(self,payload,**kwargs):
  checkpoint=original(self,payload,**kwargs)
  trigger={'parent':payload.parent_decision is not None,'child':payload.child_id is not None,'test':payload.test is not None}[sys.argv[2]]
  if trigger: os._exit(85)
  return checkpoint
 RunServices.checkpoint=stop
result=run_recipe(recipe,registry=registry,lock=lock,root='run',resume=sys.argv[1]=='resume',artifact_source='study/artifact-store')
assert result.status.value=='completed',result.diagnostic
report=report_run('run',include_private=True)
assert len(report['tasks'])==9 and report['accounting']['actual']['evaluations']==9
assert next(row['parent_ids'] for row in report['candidates'] if row['candidate_id']=='child')==['balanced']
assert abs(next(row['score'] for row in report['tasks'] if row['purpose']=='test')-.45)<1e-9
'''
    (project/'recover.py').write_text(program)
    stopped = subprocess.run([sys.executable, 'recover.py', 'stop', boundary], cwd=project, env=env,
                             capture_output=True, text=True, timeout=60)
    assert stopped.returncode == 85, stopped.stdout+stopped.stderr
    restored = subprocess.run([sys.executable, 'recover.py', 'resume', boundary], cwd=project, env=env,
                              capture_output=True, text=True, timeout=60)
    assert restored.returncode == 0, restored.stdout+restored.stderr


def test_same_external_stage_recipe_accepts_another_public_actor_runtime(tmp_path):
    project = scaffold('stage', 'outside_review', tmp_path/'project')
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    subprocess.run([sys.executable, 'prepare.py'], cwd=project, env=env, check=True, capture_output=True)
    program = '''from dataclasses import replace
from pathlib import Path
from proteus.api.v1 import ActorResult,EmptyOptions,Plugin,PluginRef,PluginSpec,RuntimeIdentity,SourcePin,builtin_registry,implementation_hash,load_recipe,run_recipe,zero_usage
from proteus.recipes.export import report_run
from outside_review import register
class LocalActor:
 model_call_bound=0
 def execute(self,spec,request,workspace):
  return ActorResult(text=request.prompt,runtime=RuntimeIdentity(runtime=spec.runtime.plugin),usage=zero_usage())
registry=builtin_registry();register(registry)
registry.register(Plugin(ref=PluginRef(name='external.local_actor',version='1'),kind='actor',options=EmptyOptions,
 factory=lambda options:LocalActor(),implementation_hash=implementation_hash({'actor.py':Path(__file__)}),
 source='outside_review',capabilities=('session.fresh','actor.execute','calls.bounded')))
base=load_recipe('study/recipe.json')
changed=replace(base,actors={'author':replace(base.actors['author'],runtime=PluginSpec(plugin=PluginRef(name='external.local_actor',version='1')))})
outputs=[]
for name,recipe in [('echo',base),('external',changed)]:
 lock=registry.make_lock(recipe,[SourcePin(name=source,repository='https://example.invalid/actor-fixture',revision='0'*40)
  for source in ('proteus','outside_review')])
 result=run_recipe(recipe,registry=registry,lock=lock,root=name)
 assert result.status.value=='completed',result.diagnostic
 outputs.append(result.outputs['assessment'])
 report=report_run(name)
 assert report['accounting']['actual']['model_calls']==0
 assert report['attempts']['statuses']=={'completed':2}
assert outputs[0]==outputs[1]
'''
    (project/'actor_check.py').write_text(program)
    checked = subprocess.run([sys.executable, 'actor_check.py'], cwd=project, env=env,
                             capture_output=True, text=True, timeout=60)
    assert checked.returncode == 0, checked.stdout+checked.stderr
