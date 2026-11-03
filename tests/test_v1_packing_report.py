"""Regrade the exact private-verifier transport, including invalid output files."""
import json
from types import SimpleNamespace

import pytest

from proteus.benchmarks.coral_circle_packing import verify
from proteus.benchmarks.coral_circle_packing.report import geometry_at


@pytest.mark.parametrize('corruption', [None, 'command', 'credential_env', 'workspace'])
def test_harbor_report_compares_normalized_launch_options(tmp_path, corruption):
    from dataclasses import replace
    from proteus.benchmarks.coral_circle_packing.report import harbor_generations
    from proteus.core.serialization import ContractError, content_hash
    from proteus.workers.harbor import HarborWorkerRuntime
    from test_v1_harbor_workers import launch, options
    request = replace(launch(tmp_path), options=options(credential_env=('MODEL_KEY',)).to_dict())
    directory = tmp_path/'private/workers/a/first'
    directory.mkdir(parents=True)
    (directory/'request.json').write_text(request.to_json())
    command = HarborWorkerRuntime().command_options(request, directory)
    harbor = json.loads((directory/'harbor-request.json').read_text())
    execution = {'request_hash':content_hash(request), 'options':command.to_dict()}
    config = harbor['options']
    digest = content_hash(harbor)
    proof = {'request_hash':digest, 'project':harbor['trial_name']+'__env',
        'image_id':content_hash('image'), 'container_id':'fixture-container',
        'mounts':[{'source':request.workspace,'destination':'/workspace','type':'bind','rw':True}],
        'mount_destinations':['/workspace'], 'network_mode':'none', 'max_pids':config['max_pids'],
        'nano_cpus':config['cpus']*10**9, 'memory_bytes':config['memory_mb']*1024**2,
        'privileged':False, 'cap_drop':['ALL'], 'security_opt':['no-new-privileges:true'],
        'helpers':{name:{'sha256':execution['options']['command_files'][harbor[key]],'uid':0,'mode':0o644}
                   for key,name in (('bootstrap','container_worker.py'),('codex_bridge','codex_bridge.py'))}}
    records = {'execution.json':execution,'harbor-loaded.json':proof,
        'harbor-dispatched.json':{'request_hash':digest},
        'harbor-cleanup.json':{'request_hash':digest,'cleanup':{'verified':True,'errors':[],
                                                           'resources':[{'remaining':False}]}}}
    for name,value in records.items():
        (directory/name).write_text(json.dumps(value))
    if corruption:
        if corruption == 'workspace':
            harbor['workspace'] = '/different'
        else:
            harbor['options'][corruption].append('changed')
        (directory/'harbor-request.json').write_text(json.dumps(harbor))
    run = SimpleNamespace(root=tmp_path, read=lambda relative: json.loads((tmp_path/relative).read_text()))
    if corruption:
        with pytest.raises(ContractError, match='request differs from launch'):
            harbor_generations(run,set())
    else:
        rows = harbor_generations(run,set())
        assert len(rows) == 1 and rows[0]['container_id'] == 'fixture-container' and rows[0]['cleanup_verified']


@pytest.mark.parametrize('case',['valid','overlap','claimed_total','nonfinite','malformed','missing','oversize','bom'])
def test_saved_geometry_regrade_matches_private_verifier_main(tmp_path,monkeypatch,case):
    geometry={'centers':[[(i%6+.5)/6,(i//6+.5)/6] for i in range(26)],'radii':[1/12]*26}
    if case=='overlap':
        geometry['centers'][1]=geometry['centers'][0]
    if case=='claimed_total':
        geometry.update(sum_radii=999999,score=999999)
    if case=='nonfinite':
        geometry['centers'][0][0]=float('nan')
    data=json.dumps(geometry).encode()
    if case=='malformed':
        data=b'{not json'
    if case=='oversize':
        data+=b' '*256001
    if case=='bom':
        data=b'\xef\xbb\xbf'+data
    trial=tmp_path/'trial'
    artifact=trial/'artifacts/logs/artifacts/packing.json'
    artifact.parent.mkdir(parents=True)
    if case!='missing':
        artifact.write_bytes(data)
    def path(value):
        if value=='/logs/artifacts/packing.json':
            return artifact
        assert value=='/logs/verifier'
        return trial/'verifier'
    monkeypatch.setattr(verify,'Path',path)
    verify.main()
    recorded=json.loads((trial/'verifier/packing-verification.json').read_text())
    run=SimpleNamespace(root=tmp_path,evidence={})
    assert geometry_at(run,'trial')==recorded
