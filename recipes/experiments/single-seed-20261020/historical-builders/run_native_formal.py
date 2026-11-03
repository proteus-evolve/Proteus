"""Real Linux Pi source self-edit, frozen-per-episode activation and exact resume."""
from pathlib import Path
import hashlib,json,os,secrets,subprocess,sys,threading,time
ROOT=Path(__file__).resolve().parent;SOURCE=ROOT/'sources/coral';sys.path.insert(0,str(SOURCE))
from linux_native_sse import NativeProxy
from native_linux_binding_v2 import Actor,Options,BudgetedPi,model_configuration
from proteus.core.contracts import BudgetLimits,PluginRef,SessionPolicy
from proteus.core.native_activation import capture_activation,source_tree_hash
from proteus.core.native_inputs import read_native_inputs
from proteus.core.plugins import Plugin,implementation_hash
from proteus.core.serialization import content_hash
from proteus.examples.candidate_evaluation import plugin
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.cli import source_pin,installed_packages
from proteus.recipes.config import ActorSpec,RecipeConfig,ReferenceSpec
from proteus.recipes.manifest import SourcePin
from proteus.recipes.runner import run_recipe
LABEL='linux-native-pi-upstream-source-activation-v1';CASE=ROOT/'formal'/LABEL
ACTOR='proteus.native.native_linux_binding_v2:BudgetedPi';MARKER='PROTEUS_NATIVE_EVOLVED_V1'
def save(path,value):
    path.parent.mkdir(parents=True,exist_ok=True);temp=path.with_suffix(path.suffix+'.pending');temp.write_text(json.dumps(value,indent=2)+'\n');temp.replace(path)
def main():
    os.umask(0o077);assert not CASE.exists(),'Never overwrite a paid native attempt'
    gate=json.loads((ROOT/'qualified-linux-native-final.json').read_text());assert gate['status']=='passed' and gate['actual_SDK_error_gate']['status']=='passed'
    build=json.loads((ROOT/'native-artifacts/H0-build-proof.json').read_text());assert build['image_id']==gate['image_id'] and build['upstream_revision']=='d981de1229ef899957bbe968bc8dcda02a21f477'
    for name in ('linux_native_sse.py','native_linux_binding_v2.py'):
        assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==gate['helper_sha256'][name]
    CASE.mkdir(parents=True,mode=0o700);token=secrets.token_urlsafe(32);token_file=CASE/'native-only.token';token_file.write_text(token)
    bridge=gate['actual_Docker_bridge_gateway']
    proxy=NativeProxy(CASE/'proxy-receipts','http://127.0.0.1:62125',token,maximum_requests=96,arm=LABEL,listen_host=bridge)
    server=proxy.server();thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    models=CASE/'models';models.mkdir();models.chmod(0o755);model_file=models/'models.json'
    model_file.write_text(json.dumps(model_configuration(server.server_port),indent=2)+'\n');model_file.chmod(0o444)
    config={'label':LABEL,'case':str(CASE),'image_id':build['image_id'],'models_dir':str(models),'local_token_file':str(token_file),'host_bridge_gateway':bridge,'upstream_revision':build['upstream_revision'],'compiled_CLI_sha256':build['compiled_cli_sha256'],'H0_profile_version':2}
    config_path=CASE/'transport-config.json';save(config_path,config)
    goal=('Add --proteus-evolution-marker to the actual controlling TypeScript CLI entry packages/coding-agent/src/cli.ts in your own writable candidate source. '
        'It must print exactly '+MARKER+' and exit0 without a model request, preserving normal CLI behavior. '
        'This must be a real TypeScript CLI control-flow change, not a note, skill, alias, wrapper tool or edit to compiled dist. '
        'Make the edit in episode1 under /workspace/candidate/src/packages/coding-agent/src/cli.ts. Do not build, import or execute writable candidate code during any phase; Proteus owns boundary builds. '
        'Only in episode2, inspect/verify the newly active compiled CLI. Do not inspect credentials, environment variables or native transport configuration.')
    try:
        registry=builtin_registry();native=registry.get(PluginRef(name='proteus.native.pi',version='1'))
        helper_files={p.name:p for p in (Path(__file__),ROOT/'linux_native_sse.py',ROOT/'native_linux_binding_v2.py')}
        impl=implementation_hash(helper_files)
        registry.register(Plugin(ref=PluginRef(name=ACTOR,version='1'),kind='actor',options=Options,factory=Actor,implementation_hash=impl,source='native-runtime',capabilities=native.capabilities))
        recipe=RecipeConfig(name=LABEL,seed=0,method=plugin('proteus.native',episodes=2,max_turns=40,phase_turns={'observe':8,'propose':6,'act':20,'reflect':6},hard_max_turns=40,checkpoint_turns=2,announce_budget=True,goal=goal,arm='neutral'),actors={'agent':ActorSpec(runtime=plugin(ACTOR,config_path=str(config_path),config_hash=content_hash(config)),model='deepseek-flash',session=SessionPolicy.HANDOFF)},budget=BudgetLimits(max_concurrency=1),reference=ReferenceSpec(profile='native',deviations=(
            'New official upstreamPi0.85.1d981de12 LinuxAMD64H0, exact original npm lock with matching published0.85.1modeldata; full upstream workspace typecheck/build and actual CLI. No cachedGoogleSDK/source patch and no manual targetfeature.',
            'Only H0packaging/readableboot mode755 and an empty writable scoped SDKauthstore with readonly models.json were adapted; neither is agent self-evolution. Original failed unpaid SDKgates retained.',
            'One seed0/two episodes, normalhard40turns,8/6/20/6phase budgets,2checkpoint; external96HTTPceiling. Canonical native Pi sessionmodel_calls/cost remain unknown; actual callcount/usage owned by proxy/original Mac ledger.',
            'Native request guard is failclosed423/503 before intent/provider work if held/unavailable; no long wait inside SDK HTTP. Token-auth bridge-interface SSE emulation over single-shot bufferednonstream replies; duplicates/in-doubt rejected. This differs from ChatActor pre-provider waiting and is explicitly qualified with actual SDKsuccess/tool/usage/error cases.',
            'Frozen source within episode, boundary rebuild/validation then next-episode activation are unmodified bundledPiHarness/core transactions; actual compiledCLI/runtime witness and0extra-sandbox/model completedresume required.')))
        recipe=registry.resolve(recipe);source=source_pin('proteus',SOURCE);assert source.dirty_patch_hash is None
        helper=SourcePin(name='native-runtime',repository='local-content-addressed:LinuxPiNativeRuntime',revision=content_hash({name:hashlib.sha256(p.read_bytes()).hexdigest() for name,p in helper_files.items()}).split(':')[1])
        lock=registry.make_lock(recipe,[source,helper],packages=installed_packages())
        (CASE/'recipe.json').write_text(recipe.to_json()+'\n');(CASE/'lock.json').write_text(lock.to_json()+'\n')
        save(CASE/'full-protocol.json',{'label':LABEL,'source_sha':source.revision,'config_hash':recipe.config_hash,'lock_hash':content_hash(lock),'image_id':build['image_id'],'H0_build_proof':str(ROOT/'native-artifacts/H0-build-proof.json'),'unpaid_actual_SDK_gate':str(ROOT/'qualified-linux-native-final.json'),'goal':goal,'episodes':2,'seed':0,'max_turns':40,'hard_turns':40,'phase_turns':[8,6,20,6],'checkpoint':2,'external_HTTP_cap':96,'model':{'alias':'deepseek-flash','temperature':0,'max_tokens':8192,'thinking':'disabled'},'CPU':1,'memory_GiB':2,'PID_limit':256,'UID':1000,'model_config_sha256':hashlib.sha256(model_file.read_bytes()).hexdigest(),'model_config_readonly':True,'original_provider_key_present':False})
        handle=ROOT/'handles'/(LABEL+'.json');save(handle,{'label':LABEL,'pid':os.getpid(),'status':'running','root':str(CASE),'source_sha':source.revision,'image_id':build['image_id'],'started_epoch':time.time()})
        result=run_recipe(recipe,registry=registry,lock=lock,root=CASE/'run')
        assert result.status.value=='completed',result.diagnostic
        workspace=CASE/'run/workspace';records=read_native_inputs(workspace)
        phases=[r for r in records if r['input']['scope']=='harness_cli' and r['input']['phase'] in ('observe','propose','act','reflect')]
        first=[r for r in phases if r['input']['episode']==1];second=[r for r in phases if r['input']['episode']==2]
        assert len(first)>=4 and len(second)>=4 and all(r['outcome'] and r['outcome'].get('returncode')==0 for r in phases)
        src0={r['outcome']['activation']['source_sha256'] for r in first};src1={r['outcome']['activation']['source_sha256'] for r in second}
        cli0={r['outcome']['activation']['cli_sha256'] for r in first};cli1={r['outcome']['activation']['cli_sha256'] for r in second}
        assert len(src0)==len(src1)==1 and src0!=src1 and cli0.isdisjoint(cli1),'Source or actual compiledCLI did not activate only next episode'
        harness=workspace/'harness';state=CASE/'feature-check-state';state.mkdir();adapter=BudgetedPi(config)
        command=['--proteus-expect-source',source_tree_hash(harness/'src'),'--proteus-evolution-marker']
        intents_before=len(list((CASE/'proxy-receipts').glob('*.intent.json')))
        checked=adapter.sandbox.run(workspace,command,env={},timeout_s=300,mounts=((str(harness),'/workspace','ro'),(str(state),'/state')))
        activated=capture_activation(checked.stdout,command,'pi',required=True)
        assert checked.returncode==0 and checked.stdout.splitlines()[1:]==[MARKER]
        assert len(list((CASE/'proxy-receipts').glob('*.intent.json')))==intents_before,'Feature check must be model-free'
        sandbox_before=(CASE/'admission.jsonl').read_bytes();input_hash=content_hash(records)
        resumed=run_recipe(recipe,registry=registry,lock=lock,root=CASE/'run',resume=True)
        assert resumed==result and (CASE/'admission.jsonl').read_bytes()==sandbox_before
        assert content_hash(read_native_inputs(workspace))==input_hash
        assert len(list((CASE/'proxy-receipts').glob('*.intent.json')))==intents_before
        finished=[json.loads(p.read_text()) for p in (CASE/'proxy-receipts').glob('*.finished.json')];assert finished and all(v['status']==200 for v in finished)
        proof={'label':LABEL,'status':'passed_local_activation_and_completed_resume','source_sha':source.revision,'upstream_revision':build['upstream_revision'],'image_id':build['image_id'],'seed':0,'episodes':2,'source_sha_episode1':next(iter(src0)),'source_sha_episode2':next(iter(src1)),'actual_compiled_CLI_episode1':sorted(cli0),'actual_compiled_CLI_episode2':sorted(cli1),'feature_output':MARKER,'feature_activation':activated,'feature_check_extra_model_calls':0,'completed_resume_extra_sandbox_calls':0,'completed_resume_extra_native_input_calls':0,'completed_resume_extra_proxy_intents':0,'sandbox_log_sha256_before_after':hashlib.sha256(sandbox_before).hexdigest(),'transport_provider_requests':len(finished),'authoritative_original_Mac_ledger_independent_crosscheck_pending':True,'canonical_Pi_model_calls_and_cost':'unknown; do not substitute assistant samples','scientific_benchmark_quality_or_speed_claim':False}
        save(CASE/'acceptance.json',proof);save(ROOT/'results'/(LABEL+'.json'),proof);save(handle,{**proof,'pid':os.getpid(),'root':str(CASE)})
    except BaseException as exc:
        save(ROOT/'handles'/(LABEL+'.json'),{'label':LABEL,'pid':os.getpid(),'status':'failed','root':str(CASE),'diagnostic':type(exc).__name__+': '+str(exc)[:1800]});raise
    finally:server.shutdown();server.server_close();thread.join(timeout=5)
if __name__=='__main__':main()
