"""New Linux formal profiles; never resumes or rewrites a Mac experiment."""
import argparse
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parent
SOURCE=ROOT/'sources/api'
sys.path.insert(0,str(SOURCE))
from proteus.core.serialization import content_hash
from proteus.evaluation.tasks import TaskDataset
from proteus.examples.candidate_evaluation import plugin
from proteus.recipes.cli import source_pin,installed_packages
from proteus.recipes.config import ActorSpec
from proteus.recipes.manifest import SourcePin


def save(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix(path.suffix+'.pending')
    temp.write_text(json.dumps(value,indent=2)+'\n');temp.replace(path)


def main():
    os.umask(0o077)
    p=argparse.ArgumentParser();p.add_argument('--family',choices=['aime','ace','combee'],required=True)
    p.add_argument('--mode',choices=['offline','online'],default='offline')
    p.add_argument('--variant',choices=['ace','naive_parallel','combee_fixed','combee_dynamic'],default='ace')
    p.add_argument('--prepare-only',action='store_true');p.add_argument('--execute-prepared',action='store_true')
    args=p.parse_args()
    label='linux-aime-full-seed-0-v2' if args.family=='aime' else ('linux-'+args.family+'-formula-'+args.mode+('-'+args.variant if args.family=='combee' else '')+'-v2')
    root=ROOT/'formal'/label;handle=ROOT/'handles'/(label+'.json')
    os.environ['PROTEUS_RELAY_TOKEN']=(ROOT/'private-bridge/scoped-token.txt').read_text().strip()
    guard=ROOT/'guards'/(label+'.json')
    expected_guard={'version':2,'admission_url':'http://127.0.0.1:62125/admission/'+label,'token_env':'PROTEUS_RELAY_TOKEN'}
    if guard.exists():
        if json.loads(guard.read_text())!=expected_guard:raise RuntimeError('Guard identity changed')
    else:save(guard,expected_guard)
    def actor(runtime='proteus.chat',maximum=4096):
        return ActorSpec(runtime=plugin(runtime,base_url='http://127.0.0.1:62125/'+label+'/v1',
            key_env='PROTEUS_RELAY_TOKEN',timeout_seconds=600,dispatch_guard_path=str(guard)),
            model='deepseek-flash',model_settings={'temperature':0,'max_tokens':maximum,'thinking':{'type':'disabled'}})
    if args.family=='aime':
        from proteus.examples import gepa_aime as module
        settings=module.Settings(solver=actor('proteus.aime_solver',8192),reflection=actor(maximum=8192),seeds=(0,),max_metric_calls=500,minibatch_size=3)
        data_name='aime'
    else:
        from proteus.examples import ace_formula,combee_formula
        module=ace_formula if args.family=='ace' else combee_formula
        common={role:actor() for role in ('generator','reflector','curator')}
        common.update(seeds=(0,),modes=(args.mode,),max_num_rounds=3,eval_steps=100 if args.family=='ace' else 500,online_eval_frequency=15)
        if args.family=='combee':common.update(variants=(args.variant,),batch_size=50,max_workers=8)
        settings=module.Settings(**common);data_name='formula'
    data=TaskDataset.from_json((ROOT/'data'/data_name/'tasks.json').read_text())
    from collections import Counter
    expected={'train':45,'validation':45,'test':30} if data_name=='aime' else {'train':500,'validation':300,'test':200}
    if dict(Counter(r.task.split for r in data.records))!=expected:raise RuntimeError('Full split differs')
    source=source_pin('proteus',SOURCE)
    if source.dirty_patch_hash is not None:raise RuntimeError('Source dirty')
    helper=SourcePin(name='linux-launcher',repository='local-content-addressed:'+Path(__file__).name,revision=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    if not args.execute_prepared:
        if root.exists() and any(root.iterdir()):raise RuntimeError('New attempt must not overwrite a prior root')
        provenance=json.loads((ROOT/'data'/data_name/'provenance.json').read_text())
        provenance['Linux_profile']={'id':label,'host':'i-0faa491e03aae7dbd','source':source.to_dict(),
            'source_manifest':str(ROOT/'linux-source-pins.json'),'data_hash':content_hash(data),
            'shared_budget_authority':'original Mac ledger, no reset or clone','AIME_cap_usd':50,'shared_cap_usd':200,
            'old_Mac_attempts_retained':True,'old_pending_answer_not_resent':True,
            'new_full_attempt_not_darwin_resume':True,'admission_guard':expected_guard,
            'transport':'authenticated SSH reverse tunnel, stable request ID, no paid retry, full response retained before acknowledgement',
            'AIME_storage':'lossless DAG5d instead of6a snapshots' if data_name=='aime' else 'same lossless DAG5d source',
            'scientific_method_or_private_selection_changes':False}
        module.prepare(root,data,settings,sources=[source,helper],packages=installed_packages(),provenance=provenance)
        save(ROOT/'settings'/(label+'.json'),settings.to_dict())
        save(handle,{'label':label,'root':str(root),'pid':os.getpid(),'status':'prepared_not_dispatched','source_sha':source.revision,'dataset_hash':content_hash(data),'full_splits':expected})
        print(json.dumps({'label':label,'status':'prepared_not_dispatched','study':str(root/'study.json')}),flush=True)
    if args.prepare_only:return
    if not root.exists():raise RuntimeError('Prepared formal root missing')
    prior=json.loads(handle.read_text())
    if prior['status']!='prepared_not_dispatched':raise RuntimeError('No automatic retry/resume of a dispatched attempt')
    save(handle,{**prior,'pid':os.getpid(),'status':'running','started_epoch':time.time()})
    try:
        result=module.execute(root,resume=False)
        save(ROOT/'results'/(label+'.json'),result)
        save(handle,{**prior,'pid':os.getpid(),'status':'completed','finished_epoch':time.time()})
    except Exception as exc:
        save(handle,{**prior,'pid':os.getpid(),'status':'failed','diagnostic':type(exc).__name__+': '+str(exc)[:2000]})
        raise


if __name__=='__main__':main()
