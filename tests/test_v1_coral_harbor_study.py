"""The complete CORAL controller, task environment and persistent workers compose."""
from dataclasses import replace
import hashlib
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys

import pytest

from proteus.artifacts.codecs import FileDelta, FileWrite
from proteus.artifacts.store import ArtifactStore
from proteus.benchmarks.coral_circle_packing import ROOT
from proteus.benchmarks.coral_circle_packing.recipe import prepare, register
from proteus.core.contracts import BudgetLimits
from proteus.core.methods import RunServices
from proteus.core.serialization import ContractError, content_hash
from proteus.examples.candidate_evaluation import plugin
from proteus.methods.coral.report import report_coral
from proteus.recipes import load_recipe
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.runner import run_recipe
from test_v1_coral_method import cleanup, current, setup
from test_v1_evaluation import SOURCE
from test_v1_harbor_workers import live_options
from test_v1_ledger import run_ledger

FIXTURE=Path(__file__).parent/'fixtures/coral_packing_worker.py'


CRASH = r"""
import importlib,json,os,sys
from pathlib import Path
from proteus.core.methods import RunServices
from proteus.methods.coral.records import CoralState
from proteus.recipes import load_recipe
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.manifest import DependencyLock
from proteus.recipes.runner import run_recipe
root=Path(sys.argv[1])
recipe=load_recipe(root/'recipe.json')
registry=builtin_registry()
importlib.import_module(sys.argv[2]).register(registry)
lock=DependencyLock.from_json((root/'lock.json').read_text())
original=RunServices.checkpoint

def checkpoint(self,state,**kwargs):
    result=original(self,state,**kwargs)
    if isinstance(state,CoralState) and state.phase=='search' and len(state.results)==1:
        with (root/'controller-crash.json').open('w') as stream:
            stream.write(json.dumps({'exit_code':73,'state':result.to_dict(),'after_formal_results':1}))
            stream.flush()
            os.fsync(stream.fileno())
        os._exit(73)
    return result

RunServices.checkpoint=checkpoint
result=run_recipe(recipe,registry=registry,lock=lock,root=root/'run',artifact_source=root/'study/artifact-store')
raise AssertionError('controller did not reach crash boundary: '+str(result))
"""


def interrupt_controller(root,registration):
    child=subprocess.run([sys.executable,'-c',CRASH,str(root),registration],capture_output=True,text=True,timeout=1500)
    (root/'controller-crash.stdout').write_text(child.stdout)
    (root/'controller-crash.stderr').write_text(child.stderr)
    assert child.returncode==73,(child.stdout,child.stderr)
    marker=json.loads((root/'controller-crash.json').read_text())
    assert marker['after_formal_results']==1 and marker['state']['checkpoint_cursor']>0


@pytest.mark.parametrize('workers',[1,2])
def test_packing_research_worker_protocol_uses_shared_geometry_and_native_interventions(tmp_path,workers):
    recipe, registry, _=setup(tmp_path,workers=workers)
    store=ArtifactStore(tmp_path/'study/artifact-store',registry)
    initial=recipe.subject.initial_artifact
    wrapper=('import json,sys\nimport initial_program\nr=json.load(sys.stdin)\n'
             'factor=2 if sum(initial_program.run()[1])>0 else 1\n'
             'print(json.dumps({"output":r["task"]["input"]*factor,"state":r["state"]}))\n')
    artifact=store.apply_delta(initial,FileDelta(base_hash=initial.content_hash,put=(
        FileWrite.from_bytes('main.py',wrapper.encode()),
        FileWrite.from_bytes('initial_program.py',b'def run(): return [], [0], 999999\n'),
        FileWrite.from_bytes('research.py',(ROOT/'research.py').read_bytes()))))
    command=(sys.executable,'-c',FIXTURE.read_text(),'--evaluation','validation')+ (('--cooperate',) if workers==2 else ())
    options={**recipe.workers[0].runtime.options,'command':command,
        'command_files':{path:'sha256:'+hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in command[:1]}}
    recipe=registry.resolve(replace(recipe,subject=replace(recipe.subject,initial_artifact=artifact),
        workers=tuple(replace(worker,runtime=replace(worker.runtime,options=options)) for worker in recipe.workers)))
    lock=registry.make_lock(recipe,[SOURCE])
    (tmp_path/'recipe.json').write_text(recipe.to_json())
    (tmp_path/'lock.json').write_text(lock.to_json())
    services=current(tmp_path,recipe,registry,lock)
    try:
        if workers==2:
            interrupt_controller(tmp_path,'proteus.examples.coral_search')
        result=run_recipe(recipe,registry=registry,lock=lock,root=tmp_path/'run',resume=workers==2,artifact_source=tmp_path/'study/artifact-store')
        assert result.status.value=='completed',result.diagnostic
        report=report_coral(tmp_path/'run',include_private=True)
        assert report['final_cohorts'][0]['mean_score']==1
        assert len(report['generations'])==2*workers
        assert report['events']['counts']['interventions']==workers
        assert report['knowledge']['counts']['cross_worker_source_edges']==(1 if workers==2 else 0)
        if workers==2:
            assert (services.worker('b').controller.workspace/'learned.json').exists()
    finally:
        cleanup(services)


@pytest.mark.skipif(not os.environ.get('PROTEUS_HARBOR_PYTHON'),reason='requires pinned Harbor and real Docker')
@pytest.mark.parametrize('workers',[1,2])
def test_live_harbor_full_coral_packing_search_and_report(tmp_path,workers):
    from proteus.benchmarks.coral_circle_packing.report import report_packing
    root=Path(os.environ.get('PROTEUS_HARBOR_STUDY',str(tmp_path)))/f'coral-full-{workers}'
    root.mkdir(parents=True,exist_ok=True)
    sentinel=root/'private-sentinel.txt'
    sentinel.write_text('HOST_PRIVATE_GRADER_SENTINEL')
    source=FIXTURE.read_text()
    options=live_options(source,network='public')
    # Bind fixture source through the locked command, leaving the run directory
    # empty until the runner owns initialization (also in the crash subprocess).
    options=replace(options,command=(options.command[0],'-c',source,'--sentinel',str(sentinel),
        *(('--cooperate',) if workers==2 else ('--submissions-a','3'))),
                    command_files={options.command[0]:options.command_files[options.command[0]]},
                    timeout_seconds=900,setup_timeout_seconds=120,shutdown_grace_seconds=10)
    path=prepare(root/'study',os.environ['PROTEUS_HARBOR_PYTHON'],workers=workers,submissions=3,
        worker_runtime=plugin('proteus.worker.harbor',**options.to_dict()))
    recipe=load_recipe(path)
    policy=recipe.event_policies[0]
    recipe=replace(recipe,event_policies=(replace(policy,operator=replace(policy.operator,
        options={**policy.operator.options,'rules':[{'name':'reflect','trigger':'evaluations','every':1,
            'prompt':'Inspect your formal packing result and share sourced findings.'}]})),),
        reference=replace(recipe.reference,deviations=(*recipe.reference.deviations,
            'Deterministic provider fixture with reflect every result; validates full container composition, not model optimization.')),
        budget=BudgetLimits(max_evaluations=5,max_concurrency=workers+1))
    registry=builtin_registry()
    register(registry)
    recipe=registry.resolve(recipe)
    lock=registry.make_lock(recipe,[SOURCE])
    (root/'recipe.json').write_text(recipe.to_json())
    (root/'lock.json').write_text(lock.to_json())
    services=RunServices(run_id='run',recipe=recipe,registry=registry,root=root/'run',lock_hash=content_hash(lock))
    assert not (root/'run').exists() or not any((root/'run').iterdir())
    try:
        if workers==2:
            interrupt_controller(root,'proteus.benchmarks.coral_circle_packing.recipe')
        result=run_recipe(recipe,registry=registry,lock=lock,root=root/'run',resume=workers==2,artifact_source=path.parent/'artifact-store')
        assert result.status.value=='completed',result.diagnostic
        report=report_packing(root/'run',include_private=True)
        assert report['cooperation']['worker_count']==workers
        timing=report['cooperation']['timing']
        assert timing['unknown_intervals']==(1 if workers==2 else 0)
        assert (timing['continuous_run_seconds'] is None)==(workers==2)
        assert (timing['controller_seconds'] is None)==(workers==2)
        assert timing['known_controller_seconds']>0
        assert report['cooperation']['events']['counts']['interventions']==2
        assert len(report['cooperation']['generations'])==workers+2
        assert report['cooperation']['knowledge']['counts']['cross_worker_source_edges']==(1 if workers==2 else 0)
        assert report['cooperation']['final_cohorts'][0]['mean_score']==pytest.approx((26/12)/2.635977)
        assert len(report['geometry'])==5
        assert len(report['worker_containers'])==workers+2
        ledger=run_ledger(root/'run')
        before=ledger.events()
        assert run_recipe(recipe,registry=registry,lock=lock,root=root/'run',resume=True)==result
        assert ledger.events()==before
        after=report_packing(root/'run',include_private=True)
        assert after['cooperation']['timing']['continuous_run_seconds']==timing['continuous_run_seconds']
        assert after['cooperation']['timing']['controller_seconds']==timing['controller_seconds']
        assert after['geometry']==report['geometry'] and after['cooperation']['accounting']==report['cooperation']['accounting']
        (root/'private-report.json').write_text(json.dumps(after,indent=2))
        output=root/'public-report.json'
        subprocess.run([sys.executable,'-m','proteus.benchmarks.coral_circle_packing.report',str(root/'run'),
                        '--out',str(output)],check=True,capture_output=True)
        public=json.loads(output.read_text())
        assert all(row['score'] is None for row in public['geometry'] if row['visibility']=='hidden')
        assert public['cooperation']['final_cohorts'][0]['mean_score'] is None
        (root/'verified.json').write_text(json.dumps({'workers':workers,'generations':workers+2,
            'formal_submissions':3,'evaluations':5,'native_interventions':2,
            'controller_hard_exit':workers==2,'no_repeat_resume':True,'report_hash':after['report_hash']}))
        if workers==1:
            damaged=tmp_path/'damaged-run'
            shutil.copytree(root/'run',damaged)
            geometry=next(damaged.glob('evaluations/*/trials/*/private/trials/*/artifacts/logs/artifacts/packing.json'))
            response=next(damaged.glob('evaluations/*/trials/*/private/response.json'))
            worker_proof=next(damaged.glob('private/workers/*/*/harbor-loaded.json'))
            for target, mutate in ((geometry,lambda value:value.update(radii=[999]*26)),
                    (response,lambda value:value.update(request_hash=content_hash('wrong'))),
                    (worker_proof,lambda value:value.update(mounts=[]))):
                original=target.read_bytes()
                value=json.loads(original)
                mutate(value)
                target.write_text(json.dumps(value))
                with pytest.raises(ContractError):
                    report_packing(damaged,include_private=True)
                target.write_bytes(original)
        if workers==2 and (root.parent/'coral-full-1/run/result.json').exists():
            from proteus.methods.coral.report import compare_coral
            from proteus.recipes.study import StudyCase, StudySpec
            spec=StudySpec(title='Scripted container composition with equal formal budgets',mode='reference_profiles',
                seeds=(0,),cases=tuple(StudyCase(condition=str(count),seed=0,run=f'coral-full-{count}/run',
                    evaluation_ids=('confirmation',)) for count in (1,2)))
            comparison=compare_coral(spec,base=root.parent,include_private=True)
            assert all(row['budget']['max_evaluations']==5 for row in comparison['cooperation_rows'])
            assert all(row['evaluations']==5 for row in comparison['comparison']['rows'])
            (root.parent/'coral-full-study.json').write_text(spec.to_json())
            (root.parent/'coral-full-comparison.json').write_text(json.dumps(comparison,indent=2))
    finally:
        cleanup(services)
