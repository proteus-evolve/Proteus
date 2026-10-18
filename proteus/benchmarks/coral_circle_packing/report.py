"""Regrade saved circle-packing geometry and verify task/worker container evidence."""
import argparse
import json
import math
from pathlib import Path

from proteus.artifacts.codecs import bytes_hash
from proteus.artifacts.store import ArtifactStore, _read
from proteus.benchmarks.coral_circle_packing import ROOT, reference
from proteus.benchmarks.coral_circle_packing.recipe import INSTRUCTION, build_spec
from proteus.benchmarks.coral_circle_packing.verify import BENCHMARK, TOLERANCE, grade
from proteus.core.contracts import require
from proteus.core.run_lock import run_lock
from proteus.core.serialization import content_hash
from proteus.evaluation.records import TrialRecord
from proteus.execution.builds import BuildReceipt
from proteus.examples.harbor_evaluation import IMAGE
from proteus.methods.coral.report import report_coral
from proteus.recipes.export import SavedRun, report_run
from proteus.workers.records import WorkerLaunch


def raw(run, relative):
    path=run.root/relative
    require(path.resolve().is_relative_to(run.root), 'packing evidence path escapes run')
    data=_read(path)
    run.evidence[relative]=bytes_hash(data)
    return data


def cleanup_verified(record):
    require(record['verified'] and not record['errors'] and record['resources']
            and all(not item['remaining'] for item in record['resources']), 'owned resources were not cleaned')


def unique_container(containers, identity):
    require(identity and identity not in containers, 'container reused across isolated executions')
    containers.add(identity)


def harbor_generations(run, containers):
    rows=[]
    for path in sorted((run.root/'private/workers').glob('*/*/request.json')):
        prefix=path.parent.relative_to(run.root).as_posix()
        launch=WorkerLaunch.from_dict(run.read(prefix+'/request.json'))
        if launch.runtime.name!='proteus.worker.harbor':
            continue
        request=run.read(prefix+'/harbor-request.json')
        execution=run.read(prefix+'/execution.json')
        require(request['request_hash']==execution['request_hash']==content_hash(launch)
                and request['options']==dict(launch.options) and request['workspace']==launch.workspace,
                'Harbor worker request differs from launch')
        require(request['trial_name']=='proteus-'+content_hash(launch).split(':')[1][:32], 'worker container identity differs')
        digest=content_hash(request)
        proof=run.read(prefix+'/harbor-loaded.json')
        cleanup=run.read(prefix+'/harbor-cleanup.json')
        require(proof['request_hash']==cleanup['request_hash']==digest
                and run.read(prefix+'/harbor-dispatched.json')['request_hash']==digest,
                'worker dispatch/load/cleanup bindings differ')
        project=request['trial_name']+'__env'
        options=launch.options
        require(proof['project']==project and proof['image_id'].startswith('sha256:')
                and proof['mounts']==[{'source':launch.workspace,'destination':'/workspace','type':'bind','rw':True}]
                and proof['mount_destinations']==['/workspace'], 'worker workspace isolation differs')
        require(proof['network_mode']==('none' if options['network']=='none' else project+'_default')
                and proof['max_pids']==options['max_pids'] and proof['nano_cpus']==options['cpus']*10**9
                and proof['memory_bytes']==options['memory_mb']*1024**2, 'worker resources differ')
        require(not proof['privileged'] and 'ALL' in proof['cap_drop']
                and any(item.startswith('no-new-privileges') for item in proof['security_opt']), 'worker privileges differ')
        require(proof['helpers']=={name:{'sha256':execution['options']['command_files'][request[key]],'uid':0,'mode':0o644}
            for key,name in (('bootstrap','container_worker.py'),('codex_bridge','codex_bridge.py'))},
            'worker bootstrap bytes differ')
        cleanup_verified(cleanup['cleanup'])
        unique_container(containers,proof['container_id'])
        rows.append({'worker_id':launch.identity.worker_id,'operation_id':launch.operation_id,
                     'request_hash':content_hash(launch),'container_id':proof['container_id'],
                     'image_id':proof['image_id'],'cleanup_verified':True})
    return rows


def geometry_at(run, prefix):
    relative=prefix+'/artifacts/logs/artifacts/packing.json'
    path=run.root/relative
    if not path.exists():
        run.evidence[relative+'#missing']=content_hash('absent')
        return {'score':0.0,'details':'Missing or malformed packing output'}
    data=raw(run,relative)
    try:
        if len(data)>256000:
            raise ValueError('packing output exceeds the domain transport limit')
        return grade(json.loads(data.decode('utf-8')))
    except (ValueError,UnicodeError):
        return {'score':0.0,'details':'Missing or malformed packing output'}


def report_packing(root, *, include_private=False):
    root=Path(root).resolve()
    common=report_run(root,include_private=include_private)
    cooperation=report_coral(root,include_private=include_private) if common['method']['name']=='coral.single_island' else None
    with run_lock(root):
        run=SavedRun(root)
        for summary in (common,cooperation):
            if summary is not None:
                require(summary['manifest_hash']==run.manifest.manifest_hash and summary['snapshot']==run.snapshot
                        and summary['evidence'][run.snapshot]==run.evidence[run.snapshot], 'run changed during report')
        require(run.result.status.value=='completed', 'packing report needs a completed run')
        ref=reference()
        recipe=run.recipe
        require(recipe.reference.code==ref['repository'] and recipe.reference.revision==ref['revision'],
                'packing reference identity differs')
        require(recipe.execution is not None and recipe.execution.environment.backend.plugin.name=='proteus.harbor'
                and recipe.subject.runner.plugin.name=='proteus.harbor_subject', 'packing report requires recorded Harbor trials')
        require(recipe.execution.environment.image_digest==IMAGE
                and recipe.execution.environment.build_hash==content_hash(build_spec())
                and content_hash(recipe.execution.environment.backend.options['source_build'])==content_hash(build_spec()),
                'packing build protocol differs')
        initial=ArtifactStore(run.root/'artifacts',registry=None).files(recipe.subject.initial_artifact)
        require(initial['initial_program.py'].data==(ROOT/'seed.reference.py').read_bytes()
                and initial['main.py'].data==(ROOT/'candidate.py').read_bytes(), 'packing initial program differs')
        run.accounting()
        tasks,_,missing=run.evaluations(True)
        require(not missing, 'packing report has missing evaluations')
        builds={}
        for path in sorted((root/'private/builds/attempts').glob('*/receipt.json')):
            receipt=BuildReceipt.from_dict(run.read(path.relative_to(root).as_posix()))
            require(receipt.build_hash==content_hash(build_spec()) and receipt.base_image==IMAGE, 'packing build receipt differs')
            builds[content_hash(receipt)]=receipt
        rows,containers,used_builds=[],set(),set()
        for task in tasks:
            prefix=task['prediction'].split('/predictions/')[0]+f"/trials/{task['occurrence']:06d}/private"
            request=run.read(prefix+'/request.json')
            sealed=run.read(prefix+'/worker-completed.json')
            cleanup=run.read(prefix+'/worker-cleanup.json')
            response=run.read(prefix+'/response.json') if (root/prefix/'response.json').exists() else None
            require(sealed['version']==1 and sealed['request_hash']==content_hash(request)
                    and run.read(prefix+'/worker-dispatch.json')['request_hash']==sealed['request_hash']
                    and sealed['response_hash']==content_hash(response) and sealed['cleanup_hash']==content_hash(cleanup),
                    'packing sealed trial evidence differs')
            require(cleanup==run.read(prefix+'/cleanup.json'), 'packing cleanup receipts differ')
            cleanup_verified(cleanup)
            trial=TrialRecord.from_dict(run.read(task['prediction']))
            require(request['candidate_id']==task['candidate_id'] and request['artifact']==run.candidates[task['candidate_id']].artifact.to_dict()
                    and request['task_hash']==trial.task_hash==content_hash(request['task']) and request['seed']==trial.task_seed
                    and request['image_digest']==IMAGE and request['resources']==dict(recipe.execution.environment.resources)
                    and request['runtime_hash']==recipe.execution.environment.backend.options['runtime_hash'],
                    'packing trial candidate/task binding differs')
            require(raw(run,prefix+'/task/tests/verify.py')==(ROOT/'verify.py').read_bytes()
                    and raw(run,prefix+'/task/instruction.md').decode()==INSTRUCTION
                    and raw(run,prefix+'/task/tests/test.sh')==b'#!/bin/sh\nset -eu\npython3 -I /tests/verify.py\n', 'packing verifier or task differs')
            build=BuildReceipt.from_dict(request['build_receipt'])
            require(builds.get(content_hash(build))==build and build.artifact_hash==task['artifact_hash']
                    and build.status=='completed', 'packing trial build binding differs')
            used_builds.add(content_hash(build))
            score=None
            status='unscored'
            if response is not None:
                require(response['request_hash']==content_hash(request), 'packing response request differs')
                reward=(response['result'].get('verifier_result') or {}).get('rewards') or {}
                score=reward.get('reward')
                loaded=response.get('loaded_proof')
                verifier=response.get('verifier_proof')
                if score is not None:
                    require(loaded is not None and verifier is not None, 'scored packing trial has no container proof')
                    require(loaded['candidate_id']==task['candidate_id'] and loaded['loaded_artifact_hash']==task['artifact_hash']
                            and loaded['task_hash']==request['task_hash'] and loaded['build_receipt_hash']==content_hash(build)
                            and loaded['image_id']==build.image_id!=verifier['image_id']
                            and loaded['components']==request['artifact']['components'], 'packing loaded candidate/build differs')
                    require(set(verifier['mount_destinations'])=={'/logs/verifier'}, 'packing verifier isolation differs')
                    for proof in (loaded,verifier):
                        unique_container(containers,proof['container_id'])
                    name=request['trial_name']
                    require('/' not in name and '\\' not in name and name not in ('.','..'), 'invalid saved trial name')
                    trial_prefix=prefix+'/trials/'+name
                    independent=geometry_at(run,trial_prefix)
                    recorded=run.read(trial_prefix+'/verifier/packing-verification.json')
                    require(independent==recorded and math.isclose(score,independent['score'],rel_tol=0,abs_tol=1e-12)
                            and float(raw(run,trial_prefix+'/verifier/reward.txt'))==score,
                            'saved packing reward differs from independent geometry')
                    status='regraded'
            require(score==task['score'], 'packing domain reward differs from formal grade')
            visible=include_private or task['visibility']=='observed'
            rows.append({'rollout_id':task['rollout_id'],'candidate_id':task['candidate_id'],
                'evaluation_id':task['evaluation_id'],'operation_id':task['operation_id'],'visibility':task['visibility'],
                'status':status if visible else None,'score':score if visible else None,
                'score_availability':('recorded' if score is not None else 'unavailable') if visible else 'hidden',
                'build_receipt_hash':content_hash(build),'build_cache_hit':build.cache_hit})
        worker_containers=harbor_generations(run,containers) if cooperation else []
        payload={'schema_version':1,'report_kind':'coral-circle-packing','manifest_hash':run.manifest.manifest_hash,
            'snapshot':run.snapshot,'reference':ref,'normalizer':BENCHMARK,'tolerance':TOLERANCE,
            'include_private':include_private,'run':common,'cooperation':cooperation,'geometry':rows,
            'worker_containers':worker_containers,'builds':{'recorded':len(builds),'used':len(used_builds),
                'cache_hits':sum(builds[digest].cache_hit for digest in used_builds)},
            'evidence':{path:digest for path,digest in run.evidence.items()
                        if include_private or not (path.startswith('private/') or '/private/' in path)},
            'notes':['Geometry is regraded from saved JSON without executing candidate code or starting containers.',
                     'Regrading checks evidence consistency; content hashes are not independent authenticity signatures.',
                     'Final confirmation repeats the same optimization problem and does not measure held-out generalization.',
                     'Unscored infrastructure outcomes remain unavailable; this report does not turn them into zero reward.',
                     'Scripted worker fixtures validate composition; they provide no evidence of model optimization gains.']}
        return {**payload,'report_hash':content_hash(payload)}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run',type=Path)
    parser.add_argument('--include-private',action='store_true')
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args()
    require(not args.out.resolve().is_relative_to(args.run.resolve()), 'report destination must be outside run')
    report=report_packing(args.run,include_private=args.include_private)
    args.out.parent.mkdir(parents=True,exist_ok=True)
    with args.out.open('x',encoding='utf-8') as stream:
        stream.write(json.dumps(report,indent=2,ensure_ascii=False,allow_nan=False)+'\n')
    print(args.out)


if __name__=='__main__':
    main()
