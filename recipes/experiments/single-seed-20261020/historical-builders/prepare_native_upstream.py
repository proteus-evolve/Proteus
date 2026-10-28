"""Build declared pristine upstream Linux Pi H0; no target feature is inserted."""
import hashlib,json,os
from pathlib import Path
import shutil,subprocess,time
ROOT=Path(__file__).resolve().parent
EXPECTED='d981de1229ef899957bbe968bc8dcda02a21f477'
def main():
    os.umask(0o077);upstream=ROOT/'native-upstream';context=ROOT/'native-context'
    assert subprocess.check_output(['git','-C',str(upstream),'rev-parse','HEAD'],text=True).strip()==EXPECTED
    assert hashlib.sha256((upstream/'package-lock.json').read_bytes()).hexdigest()=='e3569b99e673a0051be908f2bed57651b90a966a5a0d208913c29e6830a6a6c2'
    assert not context.exists(),'Keep prior H0 attempts'
    context.mkdir()
    archived=subprocess.Popen(['git','-C',str(upstream),'archive','--format=tar',EXPECTED],stdout=subprocess.PIPE)
    subprocess.run(['tar','-xf','-','-C',str(context)],stdin=archived.stdout,check=True);archived.stdout.close();assert archived.wait()==0
    hydration=json.loads((ROOT/'native-artifacts/model-data-hydration.json').read_text())
    for name,expected in hydration['hydrated_files'].items():
        src=upstream/'packages/ai/src/providers/data'/name
        assert hashlib.sha256(src.read_bytes()).hexdigest()==expected
    shutil.copytree(upstream/'packages/ai/src/providers/data',context/'packages/ai/src/providers/data',dirs_exist_ok=True)
    original=(ROOT/'sources/coral/environments/pi-src/boot.sh').read_text()
    boot=original.replace('DISTS="packages/tui/dist','DISTS="packages/chord/dist packages/tui/dist').replace('npm run build:offline >/state/last-build.log','GOMAXPROCS=1 GOMEMLIMIT=512MiB NODE_OPTIONS=--max-old-space-size=512 npm run build:offline >/state/last-build.log')
    assert boot!=original and 'packages/chord/dist' in boot
    (context/'.proteus-boot.sh').write_text(boot)
    (context/'.dockerignore').write_text('.git\nnode_modules\n**/node_modules\n**/dist\n')
    shutil.copy2(ROOT/'Dockerfile.native-upstream',context/'Dockerfile')
    files={p.relative_to(context).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in context.rglob('*') if p.is_file()}
    proof={'status':'building_not_qualified','pid':os.getpid(),'upstream_revision':EXPECTED,'upstream_git_tree':subprocess.check_output(['git','-C',str(upstream),'rev-parse','HEAD^{tree}'],text=True).strip(),
        'hydration':hydration,'full_derived_H0_file_manifest':files,
        'derived_H0_manifest_sha256':hashlib.sha256(json.dumps(files,sort_keys=True,separators=(',',':')).encode()).hexdigest(),
        'boot_source_sha256':hashlib.sha256(boot.encode()).hexdigest(),'Dockerfile_sha256':hashlib.sha256((context/'Dockerfile').read_bytes()).hexdigest(),
        'manual_target_feature_inserted':False,'old_Google_enum_patch_applied':False,
        'compiler_only_env':{'GOMAXPROCS':'1','GOMEMLIMIT':'512MiB','NODE_OPTIONS':'--max-old-space-size=512'},
        'build_policy':'Exact upstream npmci unchanged lock; matching published0.85.1data; check:model-data and full build:offline workspace typecheck/bundle, no tsx or help-only substitution'}
    (ROOT/'native-artifacts/H0-prebuild-manifest.json').write_text(json.dumps(proof,indent=2)+'\n')
    command=['docker','buildx','build','--builder','default','--platform','linux/amd64','--load','--progress','plain','--iidfile',str(ROOT/'native-artifacts/image-id'),'-t','proteus-native-upstream-0851:20261020-v1',str(context)]
    started=time.monotonic()
    with (ROOT/'logs/native-upstream-build.log').open('wb') as log:
        result=subprocess.run(command,stdout=log,stderr=subprocess.STDOUT,timeout=1800)
    proof.update(build_returncode=result.returncode,full_build_wall_seconds=time.monotonic()-started)
    if result.returncode:
        proof['status']='build_failed_no_provider_calls'
    else:
        image=(ROOT/'native-artifacts/image-id').read_text().strip();proof.update(status='compiled_H0_unpaid_runtime_gate_pending',image_id=image)
        proof['actual_versions']=subprocess.check_output(['docker','run','--rm','--network','none','--entrypoint','sh',image,'-c','node --version; npm --version; node /opt/src/packages/coding-agent/dist/cli.js --version'],text=True)
        proof['compiled_cli_sha256']=subprocess.check_output(['docker','run','--rm','--network','none','--entrypoint','sha256sum',image,'/opt/src/packages/coding-agent/dist/cli.js'],text=True).split()[0]
    (ROOT/'native-artifacts/H0-build-proof.json').write_text(json.dumps(proof,indent=2)+'\n')
    print(json.dumps({k:v for k,v in proof.items() if k not in ('full_derived_H0_file_manifest','hydration')}),flush=True)
if __name__=='__main__':main()
