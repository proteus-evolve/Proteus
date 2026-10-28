"""Full three-domain Linux MCE, private final pools never supplied to search."""
from dataclasses import replace
import hashlib,json,os
from pathlib import Path
import sys,time
ROOT=Path(__file__).resolve().parent
SOURCE=ROOT/'sources/mce';sys.path.insert(0,str(SOURCE))
from live_workspace_actor import register as register_actor
from proteus.core.contracts import BudgetLimits,StatePolicy
from proteus.core.serialization import content_hash
from proteus.examples.candidate_evaluation import plugin
from proteus.examples.harbor_evaluation import IMAGE
from proteus.examples.meta_harness import build_recipe
from proteus.execution.harbor import identify_runtime
from proteus.methods.meta_harness.data import DIRECTORIES,DATASET_HASHES,dataset,save_provenance
from proteus.methods.meta_harness.method import register as register_method
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.cli import source_pin,installed_packages
from proteus.recipes.config import ActorSpec,EnvironmentSpec,ExecutionSpec,load_recipe
from proteus.recipes.manifest import SourcePin
from proteus.recipes.runner import run_recipe
LABEL='linux-mce-full-three-domains-seed42-v1'
def save(path,value):
    path.parent.mkdir(parents=True,exist_ok=True);temp=path.with_suffix(path.suffix+'.pending')
    temp.write_text(json.dumps(value,indent=2)+'\n');temp.replace(path)
def main():
    os.umask(0o077);root=ROOT/'formal'/LABEL
    assert not root.exists(),'Preserve prior attempts'
    qualified=json.loads((ROOT/'qualified-linux-mce.json').read_text());assert qualified['status']=='passed' and len(qualified['cases'])==6
    source=source_pin('proteus',SOURCE)
    assert source.revision==qualified['source_sha'] and source.dirty_patch_hash is None
    os.environ['PROTEUS_RELAY_TOKEN']=(ROOT/'private-bridge/scoped-token.txt').read_text().strip()
    guard=ROOT/'guards'/(LABEL+'.json')
    save(guard,{'version':2,'admission_url':'http://127.0.0.1:62125/admission/'+LABEL,'token_env':'PROTEUS_RELAY_TOKEN'})
    def actor(runtime='proteus.chat',maximum=4096):
        return ActorSpec(runtime=plugin(runtime,base_url='http://127.0.0.1:62125/'+LABEL+'/v1',key_env='PROTEUS_RELAY_TOKEN',timeout_seconds=600,dispatch_guard_path=str(guard)),model='deepseek-flash',model_settings={'temperature':0,'max_tokens':maximum,'thinking':{'type':'disabled'}})
    pools=[];pins={};orders_record={}
    for name in DIRECTORIES:
        data,orders,provenance=dataset(ROOT/'data/meta-reference',name,42)
        assert content_hash(data)==DATASET_HASHES[name]
        pools.append((name,42,data,orders));pins.update(provenance);orders_record[name]=orders
    harbor=str(ROOT/'harbor-venv/bin/python');identity=identify_runtime(harbor)
    execution=ExecutionSpec(environment=EnvironmentSpec(backend=plugin('proteus.harbor_code',python=harbor,runtime_hash=identity['runtime_hash'],setup_timeout_seconds=300,bridge_timeout_seconds=900),provider='docker',image_digest=IMAGE,resources={'cpus':1,'memory_mb':512},network='none',lifecycle='sequence'),state_policy=StatePolicy.SEQUENCE,timeout_seconds=600)
    path=build_recipe(root,pools,actor(),actor('pilot.deepseek_workspace',8192),execution,iterations=1,mode='offline',num_epochs=1,batch_size=1)
    recipe=load_recipe(path)
    recipe=replace(recipe,budget=BudgetLimits(max_model_calls=10000,max_evaluations=5000,max_concurrency=1),reference=replace(recipe.reference,deviations=tuple(recipe.reference.deviations)+(
        'New explicit Linux full single-seed common-DeepSeek profile, not a resumed Mac or original-paper model result. Full three domains, seed42, NoMemory/FewShotAll plus genuine proposals, learn always executed, batch1/oneepoch/oneouteriteration.',
        'Same bounded JSON-file proposer as retained Mac profile, content-pinned. No interactive shell for proposer; generated candidate code executes in fresh credential-free no-network Harbor containers. Private final is excluded from search.',
        'Actual Linux Docker CLI transport without Mac shim/local APIIO, exact CFS1CPU/512MiB/PID64/UID65534/no host mounts;6fresh learn/predict containers with strictcleanup and zero-dispatch completedresume passed. Uniform RPC600s/setup300s/bridge900s; unchanged private metrics and selected-system rules.',
        'Authenticated reverse SSH relay; original physical Mac ledger and global200cap/50AIME protection, existing unknowns retained. No automatic paid retry or platform-spoofed lock.')))
    registry=builtin_registry();register_method(registry);register_actor(registry);recipe=registry.resolve(recipe)
    helper=ROOT/'live_workspace_actor.py'
    helper_pin=SourcePin(name='pilot-runtime',repository='local-content-addressed:'+str(helper),revision=hashlib.sha256(helper.read_bytes()).hexdigest())
    launcher_pin=SourcePin(name='linux-launcher',repository='local-content-addressed:'+Path(__file__).name,revision=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    lock=registry.make_lock(recipe,[source,helper_pin,launcher_pin],packages=installed_packages())
    path.write_text(recipe.to_json()+'\n');(root/'lock.json').write_text(lock.to_json()+'\n');save_provenance(root/'data-provenance.json',pins)
    protocol={'label':LABEL,'source_sha':source.revision,'config_hash':recipe.config_hash,'lock_hash':content_hash(lock),'seed':42,'independent_replicates':1,'full_domains':{name:{split:len(ids) for split,ids in orders.items()} for name,orders in orders_record.items()},'dataset_hashes':DATASET_HASHES,'private_final_tests':412,'private_final_in_search':False,'baseline_systems':['no_memory','fewshot_all'],'iterations':1,'epochs':1,'batch_size':1,'NoMemory_learn_skipped':False,'runtime':identity,'guard':json.loads(guard.read_text()),'Linux_gate':str(ROOT/'qualified-linux-mce.json')}
    save(root/'full-protocol.json',protocol)
    handle=ROOT/'handles'/(LABEL+'.json');save(handle,{'label':LABEL,'pid':os.getpid(),'status':'running','root':str(root),'source_sha':source.revision,'config_hash':recipe.config_hash,'lock_hash':content_hash(lock),'started_epoch':time.time()})
    result=run_recipe(recipe,registry=registry,lock=lock,root=root/'run',artifact_source=root/'artifact-store')
    save(handle,{'label':LABEL,'pid':os.getpid(),'status':result.status.value,'root':str(root),'source_sha':source.revision,'diagnostic':result.diagnostic})
    if result.status.value=='completed':
        from proteus.methods.meta_harness.report import report
        save(ROOT/'results'/(LABEL+'.json'),report(root/'run',registry))
if __name__=='__main__':main()
