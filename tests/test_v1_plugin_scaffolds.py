"""Real generated-plugin executions, changed configurations and interrupted recovery."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from proteus.core.serialization import ContractError
from proteus.recipes.scaffolding import scaffold
from check_plugin_templates import check_project


@pytest.mark.parametrize('kind', ['method', 'stage', 'evaluator', 'subject'])
def test_generated_plugin_runs_with_original_services_and_recovers(tmp_path, kind, monkeypatch):
    monkeypatch.setenv('PYTHONPATH', str(Path(__file__).resolve().parents[1]))
    assert 'passed' in check_project(tmp_path, kind)


@pytest.mark.parametrize('name', ['../escape', 'class', 'sys', 'proteus', 'prepare', 'check', 'x-y', 'a'])
def test_scaffold_rejects_invalid_or_shadowing_names_before_writing(tmp_path, name):
    with pytest.raises(ContractError):
        scaffold('stage', name, tmp_path/'project')
    assert not (tmp_path/'project').exists()


def test_scaffold_never_replaces_an_existing_project(tmp_path):
    root = scaffold('stage', 'my_stage', tmp_path/'project')
    (root/'my_stage.py').write_text('researcher edits')
    with pytest.raises(ContractError, match='already exists'):
        scaffold('stage', 'my_stage', root)
    assert (root/'my_stage.py').read_text() == 'researcher edits'


def test_method_template_hard_exit_resumes_after_committed_cohort(tmp_path):
    project = scaffold('method', 'recovery_method', tmp_path/'project')
    environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    subprocess.run([sys.executable, 'prepare.py'], cwd=project, env=environment, check=True, capture_output=True)
    program = '''import json,os,sys
from pathlib import Path
from proteus.core.methods import RunServices
from proteus.core.serialization import content_hash
from proteus.core.ledger import Ledger
from proteus.recipes import load_recipe
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.manifest import SourcePin
from proteus.recipes.runner import run_recipe
from recovery_method import register
registry=builtin_registry();register(registry)
recipe=load_recipe('study/recipe.json')
pins=[SourcePin(name=name,repository='https://example.invalid/recovery-fixture',revision='0'*40)
      for name in ('proteus','recovery_method')]
lock=registry.make_lock(recipe,pins)
if sys.argv[1]=='stop':
    original=RunServices.checkpoint
    def stop(self,payload,**kwargs):
        result=original(self,payload,**kwargs)
        if payload.position==1: os._exit(85)
        return result
    RunServices.checkpoint=stop
result=run_recipe(recipe,registry=registry,lock=lock,root='run',resume=sys.argv[1]=='resume',
                  artifact_source='study/artifact-store')
assert result.status.value=='completed',result.diagnostic
ledger=Ledger(Path('run/private/ledger.sqlite3'),run_id='run',config_hash=recipe.config_hash,lock_hash=content_hash(lock))
assert ledger.balance()['charged']['evaluations']==3 and ledger.balance()['active_slots']==0
record=json.loads(Path('run/execution-0001.json').read_text())
assert len(record['candidates'])==1 and len(record['rollouts'])==3
print(json.dumps({'rollouts':len(record['rollouts']),'evaluations':3}))
'''
    (project/'recover.py').write_text(program)
    stopped = subprocess.run([sys.executable, 'recover.py', 'stop'], cwd=project, env=environment,
                             capture_output=True, text=True, timeout=60)
    assert stopped.returncode == 85, stopped.stderr
    resumed = subprocess.run([sys.executable, 'recover.py', 'resume'], cwd=project, env=environment,
                             capture_output=True, text=True, timeout=60)
    assert resumed.returncode == 0, resumed.stdout+resumed.stderr
    assert json.loads(resumed.stdout)['rollouts'] == 3
