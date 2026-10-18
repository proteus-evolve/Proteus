"""Container-worker contracts, cleanup admission and opt-in real Docker evidence."""

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from proteus.core.contracts import BudgetLimits, PluginRef, Usage
from proteus.core.ledger import Ledger
from proteus.core.serialization import ContractError, content_hash
from proteus.examples.candidate_evaluation import plugin
from proteus.examples.harbor_evaluation import IMAGE
from proteus.execution.harbor import identify_runtime
from proteus.execution.harbor_worker import docker
from proteus.workers import WorkerController, WorkerIdentity
from proteus.workers.harbor import HarborWorkerOptions, HarborWorkerRuntime
from proteus.workers import harbor_bridge
from proteus.workers.records import WorkerCompletion, WorkerLaunch, WorkerObservation
from proteus.workers.runtime import CommandWorkerRuntime, write_json
from test_v1_gateway import setup, services, client, submission
from test_v1_worker_knowledge import setup as knowledge_setup
from test_v1_evaluation import SOURCE


def options(**overrides):
    return HarborWorkerOptions(python=sys.executable, runtime_hash=content_hash('fixture'), image_digest=IMAGE,
        command=('/usr/local/bin/python3', '/workspace/provider.py'),
        command_files={'/usr/local/bin/python3': content_hash('fixture')}, user='1001:1001', **overrides)


def launch(root):
    return WorkerLaunch(identity=WorkerIdentity(run_id='test', config_hash=content_hash('config'),
        lock_hash=content_hash('lock'), worker_id='a', role='researcher'), runtime=PluginRef(name='proteus.worker.harbor', version='1'),
        options=options().to_dict(), operation_id='first', prompt='fixture', workspace=str(root/'workspace'))


def test_container_plugin_is_optional_and_requires_explicit_gateway_reachability(tmp_path):
    recipe, registry, _ = setup(tmp_path)
    workers = tuple(replace(worker, runtime=plugin('proteus.worker.harbor', **options().to_dict()))
                    for worker in recipe.workers)
    with pytest.raises(ContractError, match='gateway access'):
        registry.resolve(replace(recipe, workers=workers))
    workers = tuple(replace(worker, runtime=plugin('proteus.worker.harbor', **options(network='public').to_dict()))
                    for worker in recipe.workers)
    resolved = registry.resolve(replace(recipe, workers=workers,
        gateways=(replace(recipe.gateways[0], bind_host='0.0.0.0'),)))
    assert resolved.workers[0].runtime.plugin.name == 'proteus.worker.harbor'
    assert 'harbor' not in sys.modules


def test_gateway_bind_is_explicit_durable_and_preserves_local_client_access(tmp_path):
    recipe, registry, lock = setup(tmp_path)
    recipe = registry.resolve(replace(recipe, gateways=(replace(recipe.gateways[0], bind_host='0.0.0.0'),)))
    current = services(tmp_path, recipe, registry, lock)
    gateway = current.gateway('formal')
    with gateway.serve():
        a = client(gateway, 'a')
        ticket = a.submit(submission())
        assert json.loads((gateway.root/'endpoint.json').read_text())['host'] == '0.0.0.0'
    with gateway.serve():
        assert client(gateway, 'a').url == a.url and a.poll(ticket['ticket_id'])['status'] == 'queued'
    endpoint = json.loads((gateway.root/'endpoint.json').read_text())
    endpoint['host'] = '127.0.0.1'
    (gateway.root/'endpoint.json').write_text(json.dumps(endpoint))
    with pytest.raises(ContractError, match='identity differs'):
        with gateway.serve():
            pass


def test_generated_compose_mounts_only_the_worker_and_declares_network():
    request = {'workspace': '/owned/worker', 'options': options().to_dict()}
    service = harbor_bridge.compose(request)['services']['main']
    assert service['network_mode'] == 'none' and service['cap_drop'] == ['ALL']
    assert service['volumes'] == [{'type': 'bind', 'source': '/owned/worker', 'target': '/workspace',
                                  'bind': {'create_host_path': False}}]
    service = harbor_bridge.compose({**request, 'options': options(network='public').to_dict()})['services']['main']
    assert 'network_mode' not in service and service['extra_hosts'] == ['host.docker.internal:host-gateway']


def test_container_gateway_rewrite_retains_scoped_credential(monkeypatch):
    monkeypatch.setenv('PROTEUS_EVALUATION_URL', 'http://127.0.0.1:12345')
    monkeypatch.setenv('PROTEUS_EVALUATION_TOKEN', 'fixture-token')
    with pytest.raises(ValueError, match='offline'):
        harbor_bridge.container_environment({'options': options().to_dict()})
    value = harbor_bridge.container_environment({'options': options(network='public').to_dict()})
    assert value == {'PROTEUS_EVALUATION_URL': 'http://host.docker.internal:12345',
                     'PROTEUS_EVALUATION_TOKEN': 'fixture-token'}


def test_worker_completion_waits_for_verified_owned_resource_cleanup(tmp_path, monkeypatch):
    item = launch(tmp_path)
    runtime = HarborWorkerRuntime()
    runtime.command_options(item, tmp_path)
    request = json.loads((tmp_path/'harbor-request.json').read_text())
    completion = WorkerCompletion(request_hash=content_hash(item), stdout_hash=content_hash('out'),
        stderr_hash=content_hash('err'), status='completed', return_code=0, usage=Usage())
    terminal = WorkerObservation(ref=item.ref(), state='completed', completion=completion)
    monkeypatch.setattr(CommandWorkerRuntime, 'poll', lambda *_: terminal)
    write_json(tmp_path/'harbor-dispatched.json', {'request_hash': content_hash(request)})
    assert runtime.poll(item, tmp_path).state == 'uncertain'
    write_json(tmp_path/'harbor-cleanup.json', {'request_hash': content_hash(request), 'cleanup': {'verified': False}})
    assert runtime.poll(item, tmp_path).completion is None
    write_json(tmp_path/'harbor-cleanup.json', {'request_hash': content_hash(request), 'cleanup': {'verified': True}})
    write_json(tmp_path/'harbor-loaded.json', {'request_hash': content_hash(request)})
    assert runtime.poll(item, tmp_path) == terminal
    write_json(tmp_path/'harbor-cleanup.json', {'request_hash': content_hash('other'), 'cleanup': {'verified': True}})
    with pytest.raises(ContractError, match='another worker'):
        runtime.poll(item, tmp_path)


def test_guard_kills_its_live_worker_before_resource_audit(tmp_path, monkeypatch):
    process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)
    request = {'options': options(timeout_seconds=.1).to_dict(), 'trial_name': 'proteus-'+'a'*32}
    path = tmp_path/'request.json'
    path.write_text(json.dumps(request))
    def cleanup(name):
        assert process.wait(timeout=2) == -signal.SIGKILL
        return {'verified': True, 'resources': [], 'errors': []}
    monkeypatch.setattr(harbor_bridge, 'cleanup_owned_resources', cleanup)
    read_fd, write_fd = os.pipe()
    os.close(write_fd)
    try:
        harbor_bridge.guard(path, read_fd, process.pid)
        receipt = json.loads((tmp_path/'harbor-cleanup.json').read_text())
        assert receipt['request_hash'] == content_hash(request) and receipt['cleanup']['verified']
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()


PROVIDER = '''import json, os, signal, sys, time
from pathlib import Path
launch=json.loads(sys.stdin.readline())
assert os.getuid()!=0 and not Path('/var/run/docker.sock').exists()
assert not Path(launch['prompt'].splitlines()[0]).exists()
state=Path('session.json')
saved=json.loads(state.read_text()) if launch['session_id'] else {'id':'container-'+launch['identity']['worker_id'],'count':0}
assert launch['session_id'] is None or launch['session_id']==saved['id']
saved['count']+=1
state.write_text(json.dumps(saved))
print(json.dumps({'event':'session','session_id':saved['id'],'resumed_from':launch['session_id']}),flush=True)
stopped=False
def stop(*_):
 global stopped
 stopped=True
signal.signal(signal.SIGINT,stop)
Path('ready-'+launch['operation_id']).touch()
if launch['prompt'].splitlines()[-1]=='wait':
 while not stopped: time.sleep(.01)
print(json.dumps({'event':'result','session_id':saved['id'],'resumed_from':launch['session_id'],
 'usage':{'model_calls':0,'input_tokens':0,'output_tokens':0,'cost_usd':0}}),flush=True)
'''


def live_options(source, *, network='none'):
    python = os.environ['PROTEUS_HARBOR_PYTHON']
    runtime = identify_runtime(python)
    code = 'import hashlib; print("sha256:"+hashlib.sha256(open("/usr/local/bin/python3","rb").read()).hexdigest())'
    binary_hash = docker('run', '--rm', '--network', 'none', IMAGE, 'python3', '-c', code, timeout=120)
    return HarborWorkerOptions(python=python, runtime_hash=runtime['runtime_hash'], image_digest=IMAGE,
        command=('/usr/local/bin/python3', '/workspace/provider.py'),
        command_files={'/usr/local/bin/python3': binary_hash,
                       '/workspace/provider.py': 'sha256:'+hashlib.sha256(source.encode()).hexdigest()},
        user=f'{os.getuid()}:{os.getgid()}', timeout_seconds=180, setup_timeout_seconds=60,
        shutdown_grace_seconds=2, memory_mb=512, network=network)


def wait_live(worker, ref, *, ready=False):
    deadline = time.monotonic()+150
    while time.monotonic()<deadline:
        result = worker.poll(ref)
        if result.completion is not None:
            return result
        if ready and (worker.workspace/('ready-'+ref.operation_id)).exists():
            return result
        time.sleep(.05)
    worker.control(ref, 'stop')
    pytest.fail('Harbor worker did not reach the required state')


@pytest.mark.skipif(not os.environ.get('PROTEUS_HARBOR_PYTHON'), reason='requires pinned Harbor runtime and real Docker')
def test_live_harbor_worker_pause_native_workspace_and_cleanup(tmp_path):
    root = Path(os.environ.get('PROTEUS_HARBOR_STUDY', str(tmp_path)))/'persistent-workers'
    root.mkdir(parents=True, exist_ok=True)
    private = root/'private-sentinel.txt'
    private.write_text('PRIVATE_HOST_GRADER')
    selected = live_options(PROVIDER)
    identity = WorkerIdentity(run_id='live', config_hash=content_hash(selected), lock_hash=content_hash('lock'),
                              worker_id='a', role='researcher')
    ledger = Ledger(root/'private/ledger.sqlite3', run_id=identity.run_id,
                    config_hash=identity.config_hash, lock_hash=identity.lock_hash)
    def controller():
        return WorkerController(root, identity=identity, options=selected, ledger=ledger,
            scopes={'run': BudgetLimits(max_concurrency=1)}, runtime=HarborWorkerRuntime(),
            runtime_ref=PluginRef(name='proteus.worker.harbor', version='1'))
    worker = controller()
    (worker.workspace/'provider.py').write_text(PROVIDER)
    first = worker.start('first', str(private)+'\nwait')
    try:
        assert wait_live(worker, first, ready=True).completion is None
        worker.control(first, 'pause')
        assert wait_live(worker, first).state == 'paused'
        worker = controller()
        second = worker.start('second', str(private)+'\ncomplete', previous=first)
        completed = wait_live(worker, second)
        assert completed.state == 'completed', completed
        assert completed.completion.resumed_from == 'container-a'
        assert json.loads((worker.workspace/'session.json').read_text())['count'] == 2
        assert ledger.balance()['active_slots'] == 0
        before = ledger.events()
        assert worker.start('second', str(private)+'\ncomplete', previous=first) == second
        assert ledger.events() == before
        proofs = [json.loads(path.read_text()) for path in worker.private.glob('*/harbor-loaded.json')]
        assert len(proofs) == 2 and len({proof['container_id'] for proof in proofs}) == 2
        assert all(proof['mounts'] == [{'source': str(worker.workspace), 'destination': '/workspace',
                                       'type': 'bind', 'rw': True}] for proof in proofs)
        receipts = [json.loads(path.read_text()) for path in worker.private.glob('*/harbor-cleanup.json')]
        assert len(receipts) == 2 and all(item['cleanup']['verified'] for item in receipts)
        (root/'verified.json').write_text(json.dumps({'native_session': completed.completion.session_id,
            'generations': 2, 'container_ids': [p['container_id'] for p in proofs], 'cleanup_verified': True}))
    finally:
        current = worker.head()
        if worker.poll(current).completion is None:
            worker.control(current, 'stop')
            wait_live(worker, current)


@pytest.mark.skipif(not os.environ.get('PROTEUS_HARBOR_PYTHON'), reason='requires pinned Harbor runtime and real Docker')
def test_live_harbor_workers_share_knowledge_and_submit_formal_candidates(tmp_path):
    from proteus.examples import _knowledge_worker_fixture
    root = Path(os.environ.get('PROTEUS_HARBOR_STUDY', str(tmp_path)))/'container-worker-knowledge'
    root.mkdir(parents=True, exist_ok=True)
    recipe, registry, _ = knowledge_setup(root)
    source = Path(_knowledge_worker_fixture.__file__).read_text()
    selected = live_options(source, network='public')
    recipe = registry.resolve(replace(recipe,
        workers=tuple(replace(worker, runtime=plugin('proteus.worker.harbor', **selected.to_dict()))
                      for worker in recipe.workers),
        gateways=(replace(recipe.gateways[0], bind_host='0.0.0.0'),)))
    lock = registry.make_lock(recipe, [SOURCE])
    (root/'recipe.json').write_text(recipe.to_json())
    (root/'lock.json').write_text(lock.to_json())
    current = services(root, recipe, registry, lock)
    gateway = current.gateway('formal')
    workers = {name: current.worker(name).controller for name in ('a', 'b')}
    for worker in workers.values():
        (worker.workspace/'provider.py').write_text(source)
    with gateway.serve():
        refs = {name: worker.start('research', 'Share and evaluate.') for name, worker in workers.items()}
        try:
            deadline, index, scores = time.monotonic()+150, 0, []
            while time.monotonic()<deadline:
                result = gateway.step('claim-'+str(index), timeout_seconds=.05)
                index += 1
                if result is not None:
                    assert result.status == 'completed', result
                    scores.append(result.ticket.worker_id)
                states = {name: worker.poll(refs[name]) for name, worker in workers.items()}
                if all(state.completion is not None for state in states.values()):
                    assert all(state.state == 'completed' for state in states.values()), states
                    break
                time.sleep(.025)
            else:
                pytest.fail('container workers did not complete knowledge exchange')
            assert sorted(scores) == ['a', 'b']
            publication = json.loads((workers['b'].workspace/'publication.json').read_text())
            assert len(publication['record']['revision']['write']['sources']) == 2
            assert (workers['b'].workspace/'strategy.json').exists()
            assert current.ledger.balance()['active_slots'] == 0
            proofs = [json.loads(path.read_text()) for worker in workers.values()
                      for path in worker.private.glob('*/harbor-loaded.json')]
            assert len({proof['container_id'] for proof in proofs}) == 2
            assert all(len(proof['mounts']) == 1 for proof in proofs)
            (root/'verified.json').write_text(json.dumps({'workers': ['a', 'b'], 'formal_results': len(scores),
                'skill_sources': 2, 'container_ids': [proof['container_id'] for proof in proofs]}))
        finally:
            for name, worker in workers.items():
                if worker.poll(refs[name]).completion is None:
                    worker.control(refs[name], 'stop')
                    wait_live(worker, refs[name])
