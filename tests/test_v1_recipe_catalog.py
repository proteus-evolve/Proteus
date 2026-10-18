"""Catalog discovery, actual prepared methods and configuration-only policy ablations."""

from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from proteus.core.ledger import Ledger
from proteus.core.serialization import ContractError, content_hash
from proteus.examples.candidate_evaluation import plugin
from proteus.examples.search_policy import prepare, register
from proteus.recipes import load_recipe
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.catalog import ENTRIES, describe, prepare_example
from proteus.recipes.manifest import SourcePin
from proteus.recipes.runner import run_recipe
from check_recipe_catalog import check_example

SOURCE = SourcePin(name='proteus', repository='https://example.invalid/policy-fixture', revision='0'*40)


def test_catalog_is_lazy_and_distinguishes_examples_from_model_results():
    code = '''import json,sys
from proteus.recipes.catalog import describe
assert 'gepa' not in sys.modules and 'harbor' not in sys.modules
print(json.dumps(describe(kind='benchmark')))
'''
    completed = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, check=True)
    entries = json.loads(completed.stdout)
    assert {entry['family'] for entry in entries} == {'gepa', 'ace', 'meta-harness', 'combee', 'coral'}
    assert all(entry['result_status'] == 'model_results_pending' for entry in entries)
    assert all(entry['result_status'] == 'offline_fixture' for entry in describe(kind='example'))
    assert len({entry.id for entry in ENTRIES}) == len(ENTRIES)
    for entry in ENTRIES:
        assert (Path(__file__).resolve().parents[1]/entry.documentation).is_file()


def test_catalog_cli_and_benchmark_example_rejection_do_not_run_a_model(tmp_path):
    command = [sys.executable, '-m', 'proteus.cli', 'recipe']
    output = subprocess.check_output([*command, 'catalog', 'coral-circle-packing'], text=True)
    assert json.loads(output)[0]['requirements'][-1].startswith('Final confirmation')
    output = subprocess.run([*command, 'example', 'ace-formula', '--out', str(tmp_path/'benchmark')],
                            capture_output=True, text=True)
    assert output.returncode == 2 and 'explicit data/runtime/settings' in output.stderr
    assert not (tmp_path/'benchmark').exists()
    with pytest.raises(ContractError, match='unknown'):
        prepare_example('unknown', tmp_path/'unknown')


@pytest.mark.parametrize('name', [entry.id for entry in ENTRIES if entry.kind == 'example'])
def test_every_catalog_example_prepares_executes_and_resumes(tmp_path, name):
    if name in ('gepa', 'combee-gepa'):
        pytest.importorskip('gepa')
    assert check_example(tmp_path, name)['rollouts']


def test_only_the_final_policy_changes_selection_and_private_test_candidate(tmp_path):
    path = prepare(tmp_path/'study')
    base = load_recipe(path)
    registry = builtin_registry()
    register(registry)
    history = []
    for name, selector, chosen, private_score in [('mean', 'proteus.scalar_best', 'spiky', .8),
                                                ('worst', 'example.maximin', 'balanced', .4)]:
        recipe = registry.resolve(replace(base, search={'final': plugin(selector)}))
        lock = registry.make_lock(recipe, [SOURCE])
        root = tmp_path/name
        result = run_recipe(recipe, registry=registry, lock=lock, root=root,
                            artifact_source=tmp_path/'study/artifact-store')
        assert result.status.value == 'completed', result.diagnostic
        record = json.loads((root/'execution-0001.json').read_text())
        decision = next(row['value'] for row in record['values'] if row['ref'] == result.outputs['selection'].to_dict())
        assert decision['result']['selected'] == [chosen]
        view = next(row['value'] for row in record['values'] if row['ref'] == decision['view'])
        assert view['evaluation_id'] == 'validation' and len(view['vectors']) == 3
        assert len(view['dimensions']) == 2
        receipt = next(row['value'] for row in record['values'] if row['ref'] == result.outputs['test'].to_dict())
        assert receipt['batches'] == [] and receipt['candidate_ids'] == [chosen]
        private = root/'evaluations'/content_hash(receipt['id']).split(':')[1]/'private/batches/000000.json'
        assert json.loads(private.read_text())['rows'][0]['score'] == pytest.approx(private_score)
        ledger = Ledger(root/'private/ledger.sqlite3', run_id=name, config_hash=recipe.config_hash,
                        lock_hash=content_hash(lock))
        assert ledger.balance()['charged']['evaluations'] == 7 and ledger.balance()['active_slots'] == 0
        events = ledger.events()
        assert run_recipe(recipe, registry=registry, lock=lock, root=root, resume=True) == result
        assert ledger.events() == events
        history.append(([item['artifact'] for item in record['candidates']],
                        [[row['score'] for row in batch['rows']] for batch in record['evaluations']]))
    assert history[0] == history[1]


@pytest.mark.parametrize('boundary', ['evaluation', 'selection', 'confirmation'])
def test_policy_tutorial_hard_exit_preserves_selection_and_charges(tmp_path, boundary):
    path = prepare(tmp_path/'study', policy='example.maximin')
    source = '''import os,sys
from pathlib import Path
from proteus.core.methods import RunServices
from proteus.core.ledger import Ledger
from proteus.core.serialization import content_hash
from proteus.examples.search_policy import register
from proteus.recipes import load_recipe
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.manifest import SourcePin
from proteus.recipes.runner import run_recipe
root=Path(sys.argv[1]);recipe=load_recipe(root/'study/recipe.json')
registry=builtin_registry();register(registry)
lock=registry.make_lock(recipe,[SourcePin(name='proteus',repository='https://example.invalid/policy-fixture',revision='0'*40)])
if sys.argv[2]=='stop':
    original=RunServices.checkpoint
    def checkpoint(self,payload,**kwargs):
        result=original(self,payload,**kwargs)
        trigger={'evaluation':payload.evaluated==2,'selection':payload.decision is not None,
                 'confirmation':payload.test is not None}[sys.argv[3]]
        if trigger: os._exit(85)
        return result
    RunServices.checkpoint=checkpoint
result=run_recipe(recipe,registry=registry,lock=lock,root=root/'run',resume=sys.argv[2]=='resume',
                  artifact_source=root/'study/artifact-store')
assert result.status.value=='completed',result.diagnostic
ledger=Ledger(root/'run/private/ledger.sqlite3',run_id='run',config_hash=recipe.config_hash,lock_hash=content_hash(lock))
assert ledger.balance()['charged']['evaluations']==7 and ledger.balance()['active_slots']==0
assert result.state.state.payload['chosen']=='balanced'
'''
    environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    assert path.is_file()
    stopped = subprocess.run([sys.executable, '-c', source, str(tmp_path), 'stop', boundary],
                             env=environment, capture_output=True, text=True, timeout=60)
    assert stopped.returncode == 85, stopped.stdout+stopped.stderr
    resumed = subprocess.run([sys.executable, '-c', source, str(tmp_path), 'resume', boundary],
                             env=environment, capture_output=True, text=True, timeout=60)
    assert resumed.returncode == 0, resumed.stdout+resumed.stderr
