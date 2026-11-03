"""Managed code isolation: policy/transport checks and real Docker release evidence."""

import json
import os
from dataclasses import replace
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from proteus.artifacts.codecs import FileContent
from proteus.artifacts.store import ArtifactStore
from proteus.core.contracts import Outcome, PluginRef, StatePolicy
from proteus.core.methods import RunServices
from proteus.core.serialization import ContractError, canonical_json, content_hash
from proteus.examples.candidate_evaluation import plugin
from proteus.examples.code_memory import SOURCE, register
from proteus.examples.harbor_code_memory import prepare
from proteus.execution.code_records import CodeCall, CodeOptions, CodeState
from proteus.execution.harbor_code_worker import cleanup_record, compose, guard, isolation_proof
from proteus.recipes import load_recipe
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.runner import run_recipe
from test_v1_evaluation import SOURCE as SOURCE_PIN
from test_v1_ledger import run_ledger


def configured(tmp_path):
    path = prepare(tmp_path / 'study', sys.executable, runtime={'runtime_hash': content_hash('fixture')})
    registry = builtin_registry()
    register(registry)
    return registry, load_recipe(path)


def test_harbor_code_capabilities_allow_explicit_sequence_without_importing_harbor(tmp_path):
    registry, recipe = configured(tmp_path)
    resolved = registry.resolve(replace(recipe, execution=replace(recipe.execution, state_policy=StatePolicy.SEQUENCE,
        environment=replace(recipe.execution.environment, lifecycle='sequence'))))
    assert resolved.execution.environment.network == 'none'
    assert 'harbor' not in sys.modules


@pytest.mark.parametrize('change,match', [
    ({'network': 'unrestricted'}, 'no-network'),
    ({'resources': {}}, 'CPU and memory'),
    ({'resources': {'cpus': 0, 'memory_mb': 512}}, 'positive'),
])
def test_unsupported_code_container_policy_fails_before_dispatch(tmp_path, change, match):
    registry, recipe = configured(tmp_path)
    env = replace(recipe.execution.environment, **change)
    with pytest.raises(ContractError, match=match):
        registry.resolve(replace(recipe, execution=replace(recipe.execution, environment=env)))


def test_managed_code_cannot_silently_use_an_opaque_environment(tmp_path):
    registry, recipe = configured(tmp_path)
    options = {k: v for k, v in recipe.execution.environment.backend.options.items() if k != 'max_pids'}
    env = replace(recipe.execution.environment, backend=plugin('proteus.harbor', **options))
    with pytest.raises(ContractError, match='code-capable'):
        registry.resolve(replace(recipe, execution=replace(recipe.execution, environment=env)))


def test_standalone_worker_does_not_shadow_harbor_or_require_proteus(tmp_path):
    from proteus.execution.harbor_code import WORKER
    script = f'''import runpy, sys
runpy.run_path({str(WORKER)!r}, run_name='standalone_import_check')
assert 'proteus' not in sys.modules
assert {str(WORKER.parent)!r} not in sys.path
'''
    result = subprocess.run([sys.executable, '-I', '-c', script], cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_stdio_protocol_keeps_candidate_output_out_of_model_frames(tmp_path):
    from proteus.execution import code_worker
    (tmp_path / 'candidate.py').write_text(SOURCE.replace('return int(self.model',
        "print('diagnostic'); __import__('os').write(1,b'\\xff'); return int(self.model"))
    state = CodeState(artifact_hash=content_hash('stdio'))
    command = [sys.executable, '-I', code_worker.__file__, '--stdio', '200000', '5']
    process = subprocess.Popen(command, cwd=tmp_path, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, start_new_session=True)
    try:
        process.stdin.write((canonical_json({'options': CodeOptions(), 'call': CodeCall(method='predict', state=state, args=(2,))})+'\n').encode())
        process.stdin.flush()
        assert json.loads(process.stdout.readline()) == {'kind': 'model', 'ordinal': 1, 'prompt': '2'}
        process.stdin.write(b'{"kind":"model_result","ordinal":1,"text":"4"}\n')
        process.stdin.flush()
        result = json.loads(process.stdout.readline())
        stdout, stderr = process.communicate(timeout=5)
        assert result['kind'] == 'result' and result['output'][0] == 4
        assert not stdout and b'diagnostic' in stderr and b'\xff' in stderr and process.returncode == 0
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def test_cleanup_receipt_is_bound_to_dispatch_and_shared_by_all_cleanup_paths(tmp_path, monkeypatch):
    from proteus.execution import harbor_code_worker as worker
    calls = []
    def cleanup(name):
        calls.append(name)
        return {'verified': True, 'resources': [], 'errors': []}
    monkeypatch.setattr(worker, 'cleanup_owned_resources', cleanup)
    request = {'trial_name': 'proteus-' + 'a'*32}
    path = tmp_path / 'request.json'
    result = cleanup_record(path, request)
    assert cleanup_record(path, request) == result and calls == [request['trial_name']]
    with pytest.raises(ValueError, match='identity'):
        cleanup_record(path, {**request, 'trial_name': 'proteus-'+'b'*32})


def test_independent_guard_stops_its_worker_group_before_cleanup(tmp_path, monkeypatch):
    from proteus.execution import harbor_code_worker as worker
    process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], start_new_session=True)
    request = {'deadline': time.monotonic()+5, 'parent_pid': os.getpid()}
    path = tmp_path / 'request.json'
    path.write_text(json.dumps(request))
    calls = []
    monkeypatch.setattr(worker, 'cleanup_record', lambda p, r: calls.append((p, r)))
    read_fd, write_fd = os.pipe()
    os.close(write_fd)
    try:
        guard(path, read_fd, process.pid)
        assert process.wait(timeout=2) == -signal.SIGKILL and calls == [(path, request)]
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()


def test_guard_stops_a_late_mutator_after_its_worker_has_exited(tmp_path, monkeypatch):
    from proteus.execution import harbor_code_worker as worker
    marker = tmp_path/'late-resource'
    child = f'import time; from pathlib import Path; time.sleep(.7); Path({str(marker)!r}).write_text("late")'
    command = f'import subprocess,sys; subprocess.Popen([sys.executable,"-c",{child!r}])'
    process = subprocess.Popen([sys.executable, '-c', command], start_new_session=True)
    process.wait(timeout=3)
    request = {'deadline':time.monotonic()+5,'parent_pid':os.getpid()}
    path = tmp_path/'request.json'
    path.write_text(json.dumps(request))
    (tmp_path/'worker-finished.json').write_text(json.dumps({'request_hash':content_hash(request)}))
    calls = []
    monkeypatch.setattr(worker, 'cleanup_record', lambda p,r: calls.append(not marker.exists()))
    read_fd, write_fd = os.pipe()
    os.close(write_fd)
    try:
        guard(path, read_fd, process.pid)
        time.sleep(.8)
        assert calls == [True] and not marker.exists()
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_code_source_build_failure_is_invalid_candidate_and_never_dispatches(tmp_path, monkeypatch):
    from proteus.execution import harbor
    from proteus.execution.builds import SourceBuildSpec
    from proteus.evaluation.runners import InvalidCandidate
    registry, recipe = configured(tmp_path)
    env = recipe.execution.environment
    spec = SourceBuildSpec(commands=('exit 1',))
    env = replace(env, backend=plugin('proteus.harbor_code', **dict(env.backend.options), source_build=spec.to_dict()),
                  build_hash=content_hash(spec))
    recipe = registry.resolve(replace(recipe, execution=replace(recipe.execution, environment=env)))
    services = RunServices(recipe=recipe, registry=registry, run_id='failed-build', root=tmp_path/'run')
    services.artifacts.copy_from(ArtifactStore(tmp_path/'study/artifact-store', registry), recipe.subject.initial_artifact)
    monkeypatch.setattr(harbor, 'identify_runtime', lambda python: {'runtime_hash':env.backend.options['runtime_hash']})
    def fail(*args, **kwargs):
        raise InvalidCandidate('candidate build command failed')
    monkeypatch.setattr(services.builds, 'execute', fail)
    candidate = services.initial_candidate()
    state = CodeState(artifact_hash=candidate.artifact.content_hash)
    result = services.invoke_candidate(candidate, CodeCall(method='predict',state=state,args=(2,)))
    assert result.status == Outcome.INVALID_CANDIDATE and result.state == state
    assert not services.actor_calls and not list((tmp_path/'run').rglob('code-private'))


def test_code_source_build_keeps_unknown_usage_and_refuses_a_finite_global_call_cap(tmp_path, monkeypatch):
    from proteus.execution import harbor
    from proteus.execution.builds import SourceBuildSpec
    registry, recipe = configured(tmp_path)
    env = recipe.execution.environment
    spec = SourceBuildSpec(commands=('true',))
    env = replace(env, backend=plugin('proteus.harbor_code', **dict(env.backend.options), source_build=spec.to_dict()),
                  build_hash=content_hash(spec))
    recipe = registry.resolve(replace(recipe, execution=replace(recipe.execution, environment=env)))
    services = RunServices(recipe=recipe, registry=registry, run_id='bounded-build', root=tmp_path/'run')
    services.artifacts.copy_from(ArtifactStore(tmp_path/'study/artifact-store', registry), recipe.subject.initial_artifact)
    monkeypatch.setattr(harbor, 'identify_runtime', lambda python: {'runtime_hash':env.backend.options['runtime_hash']})
    candidate = services.initial_candidate()
    result = services.invoke_candidate(candidate, CodeCall(method='predict',
        state=CodeState(artifact_hash=candidate.artifact.content_hash), args=(2,)))
    assert result.status == Outcome.INFRA_ERROR and 'finite model_calls reservation' in result.diagnostic
    assert not services.actor_calls and not services.builds.records
    assert not list((tmp_path/'run').rglob('code-private'))


@pytest.mark.parametrize('change', [
    {'NetworkMode': 'bridge'}, {'Privileged': True}, {'CapDrop': []},
    {'SecurityOpt': []}, {'PidsLimit': -1}, {'PidMode': 'host'},
])
def test_isolation_witness_rejects_a_docker_policy_mismatch(monkeypatch, change):
    from proteus.execution import harbor_code_worker as worker
    settings = {'NetworkMode': 'none', 'Privileged': False, 'CapDrop': ['ALL'],
        'SecurityOpt': ['no-new-privileges:true'], 'PidsLimit': 64, 'PidMode': ''}
    monkeypatch.setattr(worker, 'docker', lambda *args: json.dumps([{'HostConfig': {**settings, **change}, 'Mounts': []}]))
    with pytest.raises(ValueError, match='isolation'):
        isolation_proof('container', {'max_pids': 64})
    assert compose({'agent_image': 'pinned', 'max_pids': 64})['services']['main']['network_mode'] == 'none'


def evidence(root, expected):
    paths = list(root.rglob('code-private/request.json'))
    assert len(paths) == expected
    for path in paths:
        request = json.loads(path.read_text())
        cleanup = json.loads((path.parent/'cleanup.json').read_text())
        assert cleanup['request_hash'] == content_hash(request) and cleanup['cleanup']['verified']
        assert all(not row['remaining'] for row in cleanup['cleanup']['resources'])
        loaded = path.parent/'loaded.json'
        if loaded.exists():
            proof = json.loads(loaded.read_text())
            assert proof['request_hash'] == content_hash(request)
            assert proof['loaded_artifact_hash'] == request['artifact']['content_hash']
            assert proof['network_mode'] == 'none' and not proof['mount_destinations']
            assert proof['candidate_user'] == '65534:65534' and proof['pids_limit'] == request['max_pids']
    return paths


LIVE = pytest.mark.skipif(not os.environ.get('PROTEUS_HARBOR_PYTHON'), reason='requires pinned Harbor runtime and live Docker')


@LIVE
def test_live_harbor_code_memory_isolated_prediction_learning_and_resume(tmp_path, monkeypatch):
    root = Path(os.environ.get('PROTEUS_HARBOR_STUDY', str(tmp_path))) / 'managed-code'
    recipe = load_recipe(prepare(root/'study', os.environ['PROTEUS_HARBOR_PYTHON']))
    monkeypatch.setenv('PROTEUS_CODE_PRIVATE_KEY', 'must-not-enter-container')
    canary = root/'host-only-canary'
    canary.write_text('host secret')
    checks = f'''import os, socket
from pathlib import Path
assert os.getuid() == 65534
assert 'PROTEUS_CODE_PRIVATE_KEY' not in os.environ
assert not Path({str(canary)!r}).exists()
assert not Path('/var/run/docker.sock').exists()
assert not Path('prior-invocation').exists()
Path('prior-invocation').write_text('ephemeral')
try:
    socket.create_connection(('1.1.1.1', 80), timeout=.2)
except OSError:
    pass
else:
    raise AssertionError('network escaped')
'''
    registry = builtin_registry()
    register(registry)
    store = ArtifactStore(root/'study/artifact-store', registry)
    artifact = store.put(PluginRef(name='proteus.file_tree',version='1'), {'candidate.py': FileContent((checks+SOURCE).encode())})
    recipe = registry.resolve(replace(recipe, subject=replace(recipe.subject, initial_artifact=artifact)))
    lock = registry.make_lock(recipe, [SOURCE_PIN])
    result = run_recipe(recipe, registry=registry, lock=lock, root=root/'run', artifact_source=store.root)
    assert result.status.value == 'completed', result.diagnostic
    records = json.loads((root/'run/execution-0001.json').read_text())
    assert len(records['actor_calls']) == 7 and len(records['rollouts']) == 5
    assert [r['output'] for r in records['rollouts']] == [2,3,4,6,14]
    paths = evidence(root/'run', 6)
    assert len({json.loads((p.parent/'loaded.json').read_text())['container_id'] for p in paths}) == 6
    assert run_recipe(recipe, registry=registry, lock=lock, root=root/'run', resume=True) == result
    assert len(json.loads((root/'run/execution-0002.json').read_text())['actor_calls']) == 7
    assert run_ledger(root/'run').balance()['active_slots'] == 0


@LIVE
def test_live_harbor_code_timeout_and_budget_preserve_memory_and_cleanup(tmp_path):
    root = Path(os.environ.get('PROTEUS_HARBOR_STUDY', str(tmp_path))) / 'managed-code-failures'
    recipe = load_recipe(prepare(root/'study', os.environ['PROTEUS_HARBOR_PYTHON']))
    from test_v1_code_protocol import CountingActor
    from proteus.core.plugins import EmptyOptions, Plugin
    registry = builtin_registry()
    register(registry)
    registry.register(Plugin(ref=PluginRef(name='fixture.counted', version='1'), kind='actor', options=EmptyOptions,
        factory=lambda options: CountingActor(), implementation_hash=content_hash('counted'), source='proteus',
        capabilities=('session.fresh','actor.execute','calls.bounded','request.timeout')))
    recipe = registry.resolve(replace(recipe, actors={'model': replace(recipe.actors['model'],runtime=plugin('fixture.counted'))}))
    store = ArtifactStore(root/'study/artifact-store', registry)
    services = RunServices(recipe=recipe, registry=registry, run_id='code-failures', root=root/'run')
    services.artifacts.copy_from(store, recipe.subject.initial_artifact)
    candidate = services.initial_candidate()
    state = CodeState(artifact_hash=candidate.artifact.content_hash)
    denied = services.invoke_candidate(candidate, CodeCall(method='predict',state=state,args=(2,)))
    assert denied.status == Outcome.INFRA_ERROR and denied.state == state
    assert not services.actor_calls  # Zero-call budget blocks actual dispatch.
    endless = store.put(PluginRef(name='proteus.file_tree',version='1'),
        {'candidate.py': FileContent(SOURCE.replace('return int(self.model', 'while True: pass\n        return int(self.model').encode())})
    timed_recipe = registry.resolve(replace(recipe, subject=replace(recipe.subject, initial_artifact=endless),
        execution=replace(recipe.execution, timeout_seconds=12)))
    services = RunServices(recipe=timed_recipe, registry=registry, run_id='code-timeout', root=root/'run-timeout')
    services.artifacts.copy_from(store, endless)
    candidate = services.initial_candidate()
    state = CodeState(artifact_hash=endless.content_hash)
    timed = services.invoke_candidate(candidate, CodeCall(method='predict',state=state,args=(2,)))
    assert timed.status == Outcome.TIMED_OUT, timed.diagnostic
    assert timed.state == state and not services.actor_calls
    evidence(root, 2)
    assert services.ledger.balance()['active_slots'] == 0


@LIVE
def test_live_harbor_code_source_build_executes_built_tree_and_reuses_receipts(tmp_path):
    import math
    from proteus.execution.builds import SourceBuildSpec
    root = Path(os.environ.get('PROTEUS_HARBOR_STUDY', str(tmp_path))) / 'managed-code-build'
    recipe = load_recipe(prepare(root/'study', os.environ['PROTEUS_HARBOR_PYTHON']))
    spec = SourceBuildSpec(commands=("sed -i 's/= model, 1/= model, 3/' candidate.py",))
    env = recipe.execution.environment
    env = replace(env, backend=plugin('proteus.harbor_code', **dict(env.backend.options), source_build=spec.to_dict()),
                  build_hash=content_hash(spec))
    registry = builtin_registry()
    register(registry)
    # Arbitrary builds retain unknown usage. Keep the model role's zero-call
    # cap while explicitly choosing an uncapped global build accounting scope.
    recipe = registry.resolve(replace(recipe, execution=replace(recipe.execution, environment=env),
        budget=replace(recipe.budget, max_model_calls=None),
        actors={'model':replace(recipe.actors['model'], budget=replace(recipe.actors['model'].budget, max_model_calls=0))}))
    store = ArtifactStore(root/'study/artifact-store', registry)
    services = RunServices(recipe=recipe, registry=registry, run_id='code-build', root=root/'run')
    services.artifacts.copy_from(store, recipe.subject.initial_artifact)
    candidate = services.initial_candidate()
    cold = services.invoke_candidate(candidate, CodeCall(method='predict',
        state=CodeState(artifact_hash=candidate.artifact.content_hash), args=(2,)))
    assert cold.status == Outcome.SUCCEEDED and cold.output[0] == 6, cold.diagnostic
    learned = services.invoke_candidate(candidate, CodeCall(method='learn_from_batch', state=cold.state,
        args=([{'input': 1, 'ground_truth': 2}],)))
    assert learned.status == Outcome.SUCCEEDED and learned.state.value == {'factor': 2}, learned.diagnostic
    assert len(services.builds.records) == 2
    assert [r.cache_hit for r in services.builds.records] == [False, True]
    assert services.builds.records[0].usage.model_calls is None
    assert services.builds.records[1].usage.model_calls == 0
    assert cold.usage.model_calls is None and learned.usage.model_calls == 0
    paths = evidence(root/'run', 2)
    images = {json.loads((p.parent/'loaded.json').read_text())['image_id'] for p in paths}
    assert images == {services.builds.records[0].image_id}
    assert all(json.loads((p.parent/'loaded.json').read_text())['build_receipt_hash'] for p in paths)
    balance = services.ledger.balance()
    assert balance['active_slots'] == 0 and balance['charged']['model_calls'] is None
    assert balance['unknown']['model_calls'] == 1 and balance['unknown']['cost_usd'] == 1
    assert math.isclose(balance['charged']['wall_seconds'], cold.usage.wall_seconds + learned.usage.wall_seconds)


CRASH_WORKER = r'''
import sys, os
from pathlib import Path
from proteus.examples.harbor_code_memory import prepare
from proteus.examples.code_memory import register
from proteus.recipes import load_recipe
from proteus.recipes.builtins import builtin_registry, EchoActor
from proteus.recipes.manifest import SourcePin
from proteus.recipes.runner import run_recipe
root, mode = Path(sys.argv[1]), sys.argv[2]
path = prepare(root/'study', os.environ['PROTEUS_HARBOR_PYTHON']) if mode=='crash' else root/'study/recipe.json'
registry = builtin_registry(); register(registry)
recipe = registry.resolve(load_recipe(path))
lock = registry.make_lock(recipe, [SourcePin(name='proteus',repository='https://example.invalid/test',revision='0'*40)])
if mode=='crash':
    def die(*args): os._exit(86)
    EchoActor.execute = die
result = run_recipe(recipe, registry=registry, lock=lock, root=root/'run',
    artifact_source=root/'study/artifact-store', resume=mode=='resume')
print(result.to_json())
'''


@LIVE
def test_live_harbor_code_host_hard_exit_cleans_container_and_refuses_uncertain_replay(tmp_path):
    root = Path(os.environ.get('PROTEUS_HARBOR_STUDY', str(tmp_path))) / 'managed-code-hard-exit'
    command = [sys.executable, '-c', CRASH_WORKER, str(root)]
    crashed = subprocess.run([*command, 'crash'], capture_output=True, text=True, timeout=180)
    assert crashed.returncode == 86, crashed.stderr
    paths = list(root.rglob('code-private/request.json'))
    assert len(paths) == 1
    path = paths[0]
    deadline = time.monotonic() + 60
    cleanup = path.parent/'cleanup.json'
    while time.monotonic() < deadline:
        if cleanup.exists() and json.loads(cleanup.read_text())['cleanup']['verified']:
            break
        time.sleep(.2)
    evidence(root, 1)
    before = path.read_bytes()
    resumed = subprocess.run([*command, 'resume'], capture_output=True, text=True, timeout=30)
    assert resumed.returncode == 0, resumed.stderr
    result = json.loads(resumed.stdout)
    assert result['status'] == 'failed' and 'RecoveryRequired' in result['diagnostic']
    assert path.read_bytes() == before and len(list(root.rglob('code-private/request.json'))) == 1
