"""Prepare ONE explicitly approved original-H0 full Naive-offline replacement.

No credential reads, admission changes, models, resume or old state import.
Original unknown request/run/cost stay quarantined. Science is unchanged; only
the new scoped root/route and infrastructure-replacement provenance differ.
"""
from collections import Counter
from dataclasses import replace
from datetime import datetime,timezone
import hashlib,json,os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parent
SOURCE=ROOT/'sources/api-ledger-serialized-io-v1'
sys.path.insert(0,str(SOURCE))
from proteus.core.serialization import content_hash
from proteus.artifacts.store import ArtifactStore
from proteus.examples import combee_formula as module
from proteus.recipes.cli import installed_packages,source_pin
from proteus.recipes.manifest import SourcePin

LABEL='linux-combee-formula-offline-naive_parallel-v7-fresh-H0'
OLD_LABEL='linux-combee-formula-offline-naive_parallel-v5-ledger-io'
H0='sha256:31b9fe5c8d690797aee55588119ac476773ac7e8535fbc1ee4520918cc1bae27'
UNKNOWN_ID='8096757dabc3ee1464a2c5ebead64a3bedf13f8057ca0463eab008a3c7c006e5'
SHA='21205d1dcdcff2a39dba31998629d22fd1ae94f9'

def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def once(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('x') as out:out.write(json.dumps(value,indent=2)+'\n')
def normalize(recipe):
    value=recipe.to_dict()
    for actor in value['actors'].values():
        actor['runtime']['options']['base_url']='<SCOPED_ROUTE>'
        actor['runtime']['options']['dispatch_guard_path']='<GUARD>'
    for evaluation in value['evaluations']:
        evaluation['tasks']['suite']['options']['path']='<FULL_PINNED_TASKS>'
    return value

def main():
    os.umask(0o077)
    audit=ROOT/'naive-v7-fresh-H0-prepared-review.json'
    new=ROOT/'formal'/LABEL;old=ROOT/'formal'/OLD_LABEL
    assert not audit.exists() and not new.exists()
    handle=json.loads((ROOT/'handles'/(OLD_LABEL+'.json')).read_text())
    assert handle['status']=='failed' and not Path('/proc/'+str(handle['pid'])).exists()
    source=source_pin('proteus',SOURCE)
    assert source.revision==SHA and source.dirty_patch_hash is None
    study,dataset,_,runs=module.load(old,check_runtime=False)
    assert len(runs)==1
    old_entry,old_recipe,old_lock=runs[0]
    protected=[old/'study.json',old/'tasks.json',old/'provenance.json',
        old/'recipes'/(old_entry.name+'.json'),old/'locks'/(old_entry.name+'.json'),
        old/'runs'/old_entry.name/'manifest.json',old/'runs'/old_entry.name/'result.json']
    original_hashes={str(p):sha(p) for p in protected}
    assert Counter(row.task.split for row in dataset.records)=={'train':500,'validation':300,'test':200}
    assert old_recipe.subject.initial_artifact.content_hash==H0
    assert study.settings.seeds==(0,) and study.settings.modes==('offline',) and study.settings.variants==('naive_parallel',)
    assert study.settings.max_workers==8 and study.settings.batch_size==50 and study.settings.eval_steps==500
    guard=ROOT/'guards'/(LABEL+'.json')
    once(guard,{'version':2,'admission_url':'http://127.0.0.1:62125/admission/'+LABEL,'token_env':'PROTEUS_RELAY_TOKEN'})
    actors={}
    for name in ('generator','reflector','curator'):
        actor=getattr(study.settings,name)
        options={**actor.runtime.options,'base_url':'http://127.0.0.1:62125/'+LABEL+'/v1','dispatch_guard_path':str(guard)}
        actors[name]=replace(actor,runtime=replace(actor.runtime,options=options))
    settings=replace(study.settings,**actors)
    provenance={'dataset_hash':content_hash(dataset),'dataset_source_provenance_hash':sha(old/'provenance.json'),
        'Linux_profile':{'id':LABEL,'source':source.to_dict()},
        'infrastructure_failure_replacement':{'replaces':OLD_LABEL,'fresh_original_H0':True,
            'resume':False,'warm_start_or_parent_learned_candidate':False,
            'old_unknown_stable_id_PERMANENTLY_QUARANTINED':UNKNOWN_ID,
            'old_unknown_original_budget_row':34086,'old_unknown_reservation_USD_retained':0.0120822,
            'old_failed_history_costs_and_data_selection_unchanged':True,
            'one_approved_original_matrix_cell_no_added_condition_or_seed_selection':True}}
    helper=SourcePin(name='naive-v7-fresh-H0-preparation',repository='local-content-addressed:'+Path(__file__).name,revision=sha(__file__))
    module.prepare(new,dataset,settings,sources=[source,helper],packages=installed_packages(),provenance=provenance)
    _,data,_,fresh=module.load(new)
    assert len(fresh)==1
    entry,recipe,lock=fresh[0]
    assert normalize(recipe)==normalize(old_recipe)
    assert content_hash(data)==content_hash(dataset)
    assert recipe.subject.initial_artifact==old_recipe.subject.initial_artifact
    assert recipe.budget==old_recipe.budget
    assert recipe.execution.max_workers==recipe.method.options['max_workers']==recipe.budget.max_concurrency==8
    assert all(actor.budget.max_concurrency==8 for actor in recipe.actors.values())
    assert all(actor.model=='deepseek-flash' and actor.model_settings['max_tokens']==4096 for actor in recipe.actors.values())
    ArtifactStore(new/'artifact-store',module.registry()).files(recipe.subject.initial_artifact)
    assert not (new/'runs').exists() and all(sha(Path(p))==h for p,h in original_hashes.items())
    files=[new/'study.json',new/'tasks.json',new/'provenance.json',new/'recipes'/(entry.name+'.json'),new/'locks'/(entry.name+'.json'),guard]
    record={'status':'prepared_ONLY_new_full_H0_replacement_NOT_ADMITTED_NOT_LAUNCHED',
        'UTC':datetime.now(timezone.utc).isoformat(),'label':LABEL,'root':str(new),
        'source':source.to_dict(),'config_hash':recipe.config_hash,'lock_hash':content_hash(lock),
        'dataset_hash':content_hash(data),'dataset_counts':dict(Counter(r.task.split for r in data.records)),
        'H0':recipe.subject.initial_artifact.to_dict(),'settings':settings.to_dict(),
        'budget':recipe.budget.to_dict(),'method_options':recipe.method.options,
        'normalized_recipe_equals_old_FULL_science_except_new_scoped_paths':True,
        'provenance':provenance,'protected_old_hashes':original_hashes,
        'files':[{'path':str(p),'sha256':sha(p)} for p in files],
        'required_admission':{'exact_new_arm':LABEL,'existing_control_change_performed':False,
            'preserve_all_existing_holds':True,'never_release_old_run_or_unknown_identity':True},
        'resource_plan':{'new_instances':0,'new_GPUs':0,'driver_slots':1,'Combee_parallel_cells':1,
            'run_and_each_role_model_call_cap':8,'shared_other_five_total_drivers':6,
            'prior_total_driver_ceiling':9,'planned_parallel_Combee_cells':3,'prior_parallel_Combee_ceiling':5,
            'candidate_execution_backend':'API subject only; no new Harbor candidate containers'},
        'financial_plan':{'original_global_cap_USD':200,'original_warning_USD':150,
            'budget_or_unknown_reservations_reset':False,'model_call_budget':recipe.budget.max_model_calls,
            'API_upper_planning_range_USD':[2,5],
            'planning_range_is_not_guarantee_or_invoice':'Historical incomplete1531 successful calls cost1.04232498 upper; full visible/private work, prompt growth and rounds vary. Original global authority remains hard safety limit.',
            'new_allowance_granted':False},
        'prepare_key_reads_provider_calls_resume_or_old_state_imports':0}
    # Contract serializes MappingProxy options without changing the frozen recipe.
    record['method_options']=recipe.to_dict()['method']['options']
    once(audit,record)
    once(ROOT/'handles'/(LABEL+'.json'),{'label':LABEL,'root':str(new),'pid':None,
        'status':'prepared_not_dispatched','source_sha':SHA,'config_hash':recipe.config_hash,'lock_hash':content_hash(lock)})
    print(json.dumps({'review':str(audit),'sha256':sha(audit),'label':LABEL,
        'config_hash':recipe.config_hash,'lock_hash':content_hash(lock),'H0':H0,'paid_calls':0}))

if __name__=='__main__':main()
