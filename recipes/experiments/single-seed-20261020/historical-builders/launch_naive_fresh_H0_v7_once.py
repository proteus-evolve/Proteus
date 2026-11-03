"""One authorized fresh-H0 Naive cell only; never old-run recovery or warm start."""
import argparse
from datetime import datetime,timezone
import hashlib,json,os
from pathlib import Path
import subprocess,sys,urllib.request

ROOT=Path(__file__).resolve().parent
LABEL='linux-combee-formula-offline-naive_parallel-v7-fresh-H0'
OLD='linux-combee-formula-offline-naive_parallel-v5-ledger-io'
SOURCE=ROOT/'sources/api-ledger-serialized-io-v1'
PIN='21205d1dcdcff2a39dba31998629d22fd1ae94f9'
H0='sha256:31b9fe5c8d690797aee55588119ac476773ac7e8535fbc1ee4520918cc1bae27'
UNKNOWN='8096757dabc3ee1464a2c5ebead64a3bedf13f8057ca0463eab008a3c7c006e5'
REVIEW=ROOT/'naive-v7-fresh-H0-prepared-review.json'
REVIEW_SHA='8048012e2ac3c2229343c85acbfe90979e0b51fad8af77f109b74b465e419721'
MARKER=ROOT/'root-acceptances/naive-v7-fresh-H0-release.json'
sys.path.insert(0,str(SOURCE))
from proteus.core.serialization import content_hash
from proteus.examples import combee_formula as module
from proteus.recipes.cli import source_pin

def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def once(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('x') as out:out.write(json.dumps(value,indent=2)+'\n')
def validate_values(review,recipe,lock):
    assert review['label']==LABEL and review['root']==str(ROOT/'formal'/LABEL)
    assert review['source']['revision']==PIN and review['source']['dirty_patch_hash'] is None
    assert content_hash(recipe)==review['config_hash'] and content_hash(lock)==review['lock_hash']
    assert recipe['subject']['initial_artifact']['content_hash']==H0
    assert recipe['seed']==0
    method=recipe['method']['options']
    assert method['mode']=='offline' and method['num_epochs']==1 and method['max_workers']==8
    assert method['batch_size']==50 and method['max_num_rounds']==3 and method['eval_steps']==500
    assert method['aggregation_mode']=='single_pass' and recipe['budget']==review['budget']
    assert recipe['execution']['max_workers']==recipe['budget']['max_concurrency']==8
    lineage=review['provenance']['infrastructure_failure_replacement']
    assert lineage['fresh_original_H0'] is True and lineage['resume'] is False
    assert lineage['warm_start_or_parent_learned_candidate'] is False
    assert lineage['old_unknown_stable_id_PERMANENTLY_QUARANTINED']==UNKNOWN
    assert review['dataset_counts']=={'train':500,'validation':300,'test':200}
    for actor in recipe['actors'].values():
        options=actor['runtime']['options']
        assert options['base_url']=='http://127.0.0.1:62125/'+LABEL+'/v1'
        assert options['dispatch_guard_path']==str(ROOT/'guards'/(LABEL+'.json'))
        assert actor['model']=='deepseek-flash' and actor['model_settings']['max_tokens']==4096
        assert actor['budget']['max_concurrency']==8
    for evaluation in recipe['evaluations']:
        assert evaluation['tasks']['suite']['options']['path']==str(ROOT/'formal'/LABEL/'tasks.json')
    assert any(pin['name']=='proteus' and pin['revision']==PIN for pin in lock['sources'])

def verify():
    assert sha(REVIEW)==REVIEW_SHA,'Frozen prepared review changed'
    review=json.loads(REVIEW.read_text())
    assert source_pin('proteus',SOURCE).revision==PIN and not source_pin('proteus',SOURCE).dirty_patch_hash
    assert all(sha(item['path'])==item['sha256'] for item in review['files'])
    assert all(sha(path)==digest for path,digest in review['protected_old_hashes'].items())
    _,_,_,runs=module.load(Path(review['root']))
    assert len(runs)==1
    entry,recipe,lock=runs[0]
    validate_values(review,recipe.to_dict(),lock.to_dict())
    handle=json.loads((ROOT/'handles'/(LABEL+'.json')).read_text())
    assert handle['status']=='prepared_not_dispatched'
    assert not (ROOT/'formal'/LABEL/'runs').exists(),'Never resume/repeat a dispatched attempt'
    return review

def accepted():
    review=verify();marker=json.loads(MARKER.read_text())
    expected={'accepted':True,'label':LABEL,'preparation_review_sha256':sha(REVIEW),
        'launcher_sha256':sha(__file__),'source_revision':PIN,'config_hash':review['config_hash'],
        'lock_hash':review['lock_hash'],'full_fresh_H0_only':True,
        'old_run_and_unknown_permanently_quarantined':True,'allow_scientific_launch':True}
    assert all(marker.get(k)==v for k,v in expected.items()),'Exact independent root release required'
    return review,marker

def launch():
    review,_=accepted()
    claim=ROOT/(LABEL+'-launch-intent.json');receipt=ROOT/(LABEL+'-launch-receipt.json')
    assert not claim.exists() and not receipt.exists()
    token=(ROOT/'private-bridge/scoped-token.txt').read_text().strip()
    for label,allowed in ((OLD,False),(LABEL,True)):
        request=urllib.request.Request('http://127.0.0.1:62125/admission/'+label,headers={'Authorization':'Bearer '+token})
        with urllib.request.urlopen(request,timeout=5) as response:assert json.load(response)['allowed'] is allowed
    del token
    once(claim,{'UTC':datetime.now(timezone.utc).isoformat(),'label':LABEL,
        'launcher_sha256':sha(__file__),'marker_sha256':sha(MARKER),'fresh_H0':True,'resume':False})
    argv=[sys.executable,'-u',str(Path(__file__)),'--execute-prepared']
    log=ROOT/'logs'/(LABEL+'.log')
    with log.open('xb') as output:
        child=subprocess.Popen(argv,cwd=ROOT,stdin=subprocess.DEVNULL,stdout=output,stderr=subprocess.STDOUT,start_new_session=True)
    once(receipt,{'UTC':datetime.now(timezone.utc).isoformat(),'pid':child.pid,'argv':argv,
        'label':LABEL,'source_revision':PIN,'config_hash':review['config_hash'],'lock_hash':review['lock_hash'],
        'root_release':str(MARKER),'launch_claim':str(claim),'resume':False,'automatic_retry':False})
    print(json.dumps({'label':LABEL,'pid':child.pid,'receipt':str(receipt)}),flush=True)

def execute():
    review,_=accepted()
    claim=json.loads((ROOT/(LABEL+'-launch-intent.json')).read_text())
    assert claim['label']==LABEL and claim['launcher_sha256']==sha(__file__) and claim['marker_sha256']==sha(MARKER)
    once(ROOT/(LABEL+'-execute-claim.json'),{'pid':os.getpid(),'UTC':datetime.now(timezone.utc).isoformat()})
    os.environ['PROTEUS_RELAY_TOKEN']=(ROOT/'private-bridge/scoped-token.txt').read_text().strip()
    handle=ROOT/'handles'/(LABEL+'.json')
    handle.write_text(json.dumps({'label':LABEL,'pid':os.getpid(),'root':review['root'],'status':'running',
        'source_sha':PIN,'config_hash':review['config_hash'],'lock_hash':review['lock_hash'],'fresh_H0':True,'resume':False},indent=2)+'\n')
    try:
        result=module.execute(Path(review['root']),resume=False)
        once(ROOT/'results'/(LABEL+'.json'),result)
        state='completed';diagnostic=None
    except BaseException as error:
        state='failed';diagnostic=type(error).__name__+': '+str(error)[:1500]
        raise
    finally:
        handle.write_text(json.dumps({'label':LABEL,'pid':os.getpid(),'root':review['root'],'status':state,
            'source_sha':PIN,'diagnostic':diagnostic,'automatic_retry':False},indent=2)+'\n')

if __name__=='__main__':
    os.umask(0o077);parser=argparse.ArgumentParser()
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--inspect-prepared',action='store_true')
    mode.add_argument('--launch-once',action='store_true')
    mode.add_argument('--execute-prepared',action='store_true')
    args=parser.parse_args()
    if args.inspect_prepared:
        review=verify();print(json.dumps({'status':'prepared_verified_not_released_or_launched',
            'review':str(REVIEW),'config_hash':review['config_hash'],'lock_hash':review['lock_hash'],
            'launcher_sha256':sha(__file__),'paid_calls':0}))
    elif args.launch_once:launch()
    else:execute()
