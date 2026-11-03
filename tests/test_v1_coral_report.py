"""Reports use completed independent process runs, without redispatching workers."""
from dataclasses import replace
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys

import pytest

from proteus.core.serialization import ContractError
from proteus.methods.coral.report import compare_coral, report_coral
from proteus.recipes.runner import run_recipe
from proteus.recipes.study import StudyCase, StudySpec
from test_v1_coral_method import cleanup, current, fixture_options, setup


def execute(root, workers=2, change=lambda recipe: recipe):
    root.mkdir(parents=True, exist_ok=True)
    recipe, registry, lock = setup(root, workers=workers, change=change)
    services = current(root, recipe, registry, lock)
    try:
        result = run_recipe(recipe, registry=registry, lock=lock, root=root/'run', artifact_source=root/'study/artifact-store')
        assert result.status.value == 'completed', result.diagnostic
    finally:
        cleanup(services)
    return recipe, registry, lock


def hashes(root):
    return {str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}


@pytest.mark.parametrize('workers', [1, 2])
def test_actual_worker_history_knowledge_private_scores_and_readonly_resume(tmp_path, workers):
    recipe, registry, lock = execute(tmp_path, workers)
    before = hashes(tmp_path/'run')
    public = report_coral(tmp_path/'run')
    report = report_coral(tmp_path/'run', include_private=True)
    assert hashes(tmp_path/'run') == before
    assert public['final_cohorts'][0]['mean_score'] is None
    assert all(not key.startswith('private/') and '/private/' not in key for key in public['evidence'])
    assert report['final_cohorts'][0]['mean_score'] == 1
    assert report['worker_count'] == workers and len(report['generations']) == 2*workers
    assert len(report['submissions']) == workers+1 and report['events']['counts']['interventions'] == workers
    assert all(row['generations']==2 and row['continuity']=={'fresh':1,'native':1} for row in report['workers'])
    assert report['knowledge']['counts']['cross_worker_source_edges'] == (1 if workers == 2 else 0)
    assert report['accounting']['actual']['evaluations'] == 2+2*(workers+1)+1
    assert report['accounting']['actual']['cost_usd'] is None
    assert 'not file delivery' in report['knowledge']['measurement']
    assert 'not full experiment wall time' in report['events']['time_semantics']
    assert report['baseline_cohorts'][0]['mean_score'] == 0
    assert run_recipe(recipe,registry=registry,lock=lock,root=tmp_path/'run',resume=True).status.value == 'completed'
    resumed = report_coral(tmp_path/'run',include_private=True)
    for field in ('generations','workers','knowledge','events','submissions','accounting','selected_candidates','final_cohorts'):
        assert resumed[field] == report[field]


def test_single_multi_worker_reference_profiles_keep_explicit_budget_differences(tmp_path):
    cases = []
    for workers in (1, 2):
        execute(tmp_path/str(workers),workers)
        cases.append(StudyCase(condition=str(workers),seed=0,run=str(workers)+'/run',evaluation_ids=('test',)))
    spec = StudySpec(title='Offline CORAL plumbing profiles', mode='reference_profiles', seeds=(0,),cases=tuple(cases))
    report = compare_coral(spec,base=tmp_path,include_private=True)
    assert report['comparison']['paired_deltas'] == []
    assert [row['worker_count'] for row in report['cooperation_rows']] == [1,2]
    assert [row['budget']['max_evaluations'] for row in report['cooperation_rows']] == [7,9]
    assert all(row['quality']==1 and row['cost_usd'] is None for row in report['comparison']['rows'])
    source = tmp_path/'study.json'
    source.write_text(spec.to_json())
    output = tmp_path/'comparison.json'
    subprocess.run([sys.executable,'-m','proteus.methods.coral.report',str(source),'--study','--include-private','--out',str(output)],check=True,capture_output=True)
    assert json.loads(output.read_text())['report_hash'] == report['report_hash']
    assert subprocess.run([sys.executable,'-m','proteus.methods.coral.report',str(source),'--study','--out',str(output)],capture_output=True).returncode != 0
    with pytest.raises(ContractError,match='undeclared'):
        compare_coral(replace(spec,mode='controlled',baseline='1'),base=tmp_path)


def test_poll_limit_and_fresh_fallback_are_reported(tmp_path):
    execute(tmp_path/'idle',1,lambda recipe: fixture_options(recipe,'--idle',max_polls=3))
    idle = report_coral(tmp_path/'idle/run')
    assert idle['stop_reason']=='poll_limit' and idle['submissions']==[]
    assert idle['events']['counts']['interventions']==0
    def change(recipe):
        return replace(recipe,event_policies=(replace(recipe.event_policies[0],resume_policy='fresh',fresh_orientation='Read candidate/ and shared notes.'),))
    execute(tmp_path/'fresh',1,change)
    fresh = report_coral(tmp_path/'fresh/run')
    assert fresh['workers'][0]['continuity']=={'fresh':1,'fresh_fallback':1}


def test_corrupted_worker_queue_knowledge_and_event_evidence_is_rejected(tmp_path):
    execute(tmp_path/'source')
    for kind in ('stream','completion','ledger','gateway','knowledge','snapshot','event'):
        root = tmp_path/kind
        shutil.copytree(tmp_path/'source/run',root)
        if kind == 'stream':
            next((root/'private/workers').glob('*/*/stdout.log')).write_bytes(b'altered')
        elif kind == 'completion':
            path = next((root/'private/workers').glob('*/*/completion.json'))
            value=json.loads(path.read_text())
            value['usage']['input_tokens']=999
            path.write_text(json.dumps(value))
        elif kind == 'ledger':
            with sqlite3.connect(root/'private/ledger.sqlite3') as db:
                db.execute("DELETE FROM entries WHERE id LIKE 'worker:%'")
        elif kind == 'gateway':
            with sqlite3.connect(next((root/'private/gateways').glob('*/queue.sqlite3'))) as db:
                db.execute('DELETE FROM closure')
        elif kind == 'knowledge':
            with sqlite3.connect(next((root/'private/knowledge').glob('*/knowledge.sqlite3'))) as db:
                db.execute('DELETE FROM heads')
        elif kind == 'snapshot':
            with sqlite3.connect(next((root/'private/knowledge').glob('*/knowledge.sqlite3'))) as db:
                db.execute('DELETE FROM snapshots')
        else:
            path = sorted(root.glob('execution-*.json'))[-1]
            value=json.loads(path.read_text())
            item=next(row for row in value['values'] if row['ref']['kind']=='proteus.event.decision@1')
            item['value']['observation']['score']=999
            path.write_text(json.dumps(value))
        with pytest.raises((ContractError,FileNotFoundError)):
            report_coral(root)


def test_cancelled_pending_submission_is_excluded_from_formal_interventions(tmp_path, monkeypatch):
    from proteus.artifacts.codecs import FileWrite
    from proteus.gateway.records import GatewaySubmission
    from proteus.gateway.service import GatewayService
    original = GatewayService.close

    def close(self, *args):
        self.submit('a',GatewaySubmission(operation_id='pending-extra',evaluation_id='validation',
            files=(FileWrite.from_bytes('main.py',b'raise AssertionError("must not run")\n'),)))
        return original(self,*args)

    monkeypatch.setattr(GatewayService,'close',close)
    execute(tmp_path,1,lambda recipe: replace(recipe,gateways=(replace(recipe.gateways[0],max_evaluations_per_worker=6),)))
    report=report_coral(tmp_path/'run')
    assert len(report['submissions'])==3
    assert sum(row['cancelled_at_close'] for row in report['submissions'])==1
    assert report['workers'][0]['formal_submissions']==2
    assert report['accounting']['actual']['evaluations']==7


def test_restart_generations_are_separate_from_planned_interventions(tmp_path):
    def change(recipe):
        recipe=fixture_options(recipe,'--exit-first',max_restarts_per_worker=1)
        policy=recipe.event_policies[0]
        return replace(recipe,event_policies=(replace(policy,operator=replace(policy.operator,
            options={**policy.operator.options,'rules':[{**rule,'every':100} for rule in policy.operator.options['rules']]})),))
    execute(tmp_path,1,change)
    report=report_coral(tmp_path/'run')
    assert report['workers'][0]['restarts']==1
    assert report['workers'][0]['generations']==2
    assert report['events']['counts']['interventions']==0


def test_recorded_cumulative_session_counters_are_not_added_as_generation_usage(tmp_path):
    from proteus.examples import _coral_worker_fixture
    from pathlib import Path
    fixture = tmp_path/'metered_worker.py'
    code = Path(_coral_worker_fixture.__file__).read_text()
    old = '"usage": {"model_calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0}}'
    new = ('"usage": {"model_calls": 1, "input_tokens": 10, "output_tokens": 2, "cost_usd": .01},'
           '"session_usage": {"input_tokens": 10 if launch["continuity"] == "fresh" else 20,'
           '"output_tokens": 2 if launch["continuity"] == "fresh" else 4}}')
    assert old in code
    fixture.write_text(code.replace(old,new))
    def change(recipe):
        worker=recipe.workers[0]
        command=(worker.runtime.options['command'][0],str(fixture))
        options={**worker.runtime.options,'command':command,
                 'command_files':{path:'sha256:'+hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in command}}
        return replace(recipe,workers=(replace(worker,runtime=replace(worker.runtime,options=options)),))
    execute(tmp_path/'study',1,change)
    report=report_coral(tmp_path/'study/run')
    assert report['workers'][0]['generation_usage']['input_tokens']==20
    assert report['workers'][0]['generation_usage']['output_tokens']==4
    assert report['workers'][0]['generation_actual']['cost_usd']==.02
    assert sorted(row['session_usage']['input_tokens'] for row in report['generations'])==[10,20]
    assert all(row['usage']['input_tokens']==10 for row in report['generations'])
