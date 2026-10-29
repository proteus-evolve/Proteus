"""Release blockers: uncertainty is retained; prepared Native commits roll forward."""

from dataclasses import replace
import os
import subprocess

import pytest

from proteus.core.contracts import BudgetLimits, OperationStatus, Outcome, RuntimeIdentity
from proteus.core.recovery import RecoveryRequired, recovery_status
from proteus.core.serialization import content_hash
from proteus.evaluation.records import SubjectResult, zero_usage
from proteus.recipes.config import ActorSpec, PluginSpec
from proteus.recipes.runner import run_recipe
from test_v1_plugins import SOURCE, plugin, stage_recipe
from test_v1_ledger import run_ledger
from test_v1_evaluation import setup as evaluation_setup


@pytest.mark.parametrize('failure', [RecoveryRequired, KeyboardInterrupt, SystemExit])
def test_uncertain_actor_never_settles_or_retries(tmp_path, failure):
    from proteus.recipes.builtins import builtin_registry
    calls = []
    class Actor:
        model_call_bound = 1
        def execute(self, spec, request, workspace):
            calls.append('dispatch')
            raise failure('external work not reconciled')
    registry = builtin_registry()
    definition = plugin('fixture.uncertain_actor', 'actor', lambda options: Actor(),
                        capabilities=('actor.execute', 'session.fresh', 'calls.bounded'))
    registry.register(definition)
    recipe = stage_recipe()
    recipe = replace(recipe, actors={'optimizer': ActorSpec(runtime=PluginSpec(plugin=definition.ref))},
                     stages=(replace(recipe.stages[0], max_attempts=2, failure_policy='retry'),),
                     budget=BudgetLimits(max_model_calls=2, max_concurrency=1))
    lock = registry.make_lock(recipe, [SOURCE])
    root = tmp_path / 'run'
    if failure is KeyboardInterrupt:
        with pytest.raises(KeyboardInterrupt):
            run_recipe(recipe, registry=registry, lock=lock, root=root)
    else:
        result = run_recipe(recipe, registry=registry, lock=lock, root=root)
        assert result.status == OperationStatus.FAILED
    book = run_ledger(root)
    before = book.events()
    for _ in range(2):
        result = run_recipe(recipe, registry=registry, lock=lock, root=root, resume=True)
        assert result.status == OperationStatus.FAILED and 'RecoveryRequired' in result.diagnostic
        assert book.events() == before and book.balance()['active_slots'] == 1
    assert calls == ['dispatch']
    assert recovery_status(root)['unsettled'][0]['state'] == 'started'


@pytest.mark.parametrize('workers', [1, 2])
def test_subject_can_reconcile_without_new_dispatch(tmp_path, workers):
    registry, store, _, recipe = evaluation_setup(tmp_path, references=(1,))
    calls = {'run': 0, 'reconcile': 0}
    ready = False
    class Subject:
        def run(self, context):
            calls['run'] += 1
            raise RecoveryRequired('worker still active')
        def reconcile(self, context):
            calls['reconcile'] += 1
            if not ready:
                raise RecoveryRequired('worker still active')
            return SubjectResult(loaded_artifact_hash=context.candidate.artifact.content_hash,
                runtime=RuntimeIdentity(runtime=context.subject.runner.plugin,
                    environment_hash=content_hash(context.execution.environment)),
                status=Outcome.SUCCEEDED, output=1, state=context.state, usage=zero_usage())
    definition = registry.get(recipe.subject.runner.plugin)
    registry._plugins[(definition.ref.name, definition.ref.version)] = replace(definition,
        factory=lambda options: Subject(), capabilities=(*definition.capabilities, 'subject.reconcile'))
    recipe = replace(recipe, execution=replace(recipe.execution, max_workers=workers),
                     budget=BudgetLimits(max_concurrency=workers))
    lock = registry.make_lock(recipe, [SOURCE])
    root = tmp_path / 'run'
    result = run_recipe(recipe, registry=registry, lock=lock, root=root, artifact_source=store.root)
    assert result.status == OperationStatus.FAILED and 'RecoveryRequired' in result.diagnostic
    book = run_ledger(root)
    before = book.events()
    result = run_recipe(recipe, registry=registry, lock=lock, root=root, resume=True)
    assert result.status == OperationStatus.FAILED and book.events() == before
    ready = True
    result = run_recipe(recipe, registry=registry, lock=lock, root=root, resume=True)
    assert result.status == OperationStatus.COMPLETED, result.diagnostic
    assert calls == {'run': 1, 'reconcile': 2}
    assert book.balance()['active_slots'] == 0


@pytest.mark.parametrize('boundary', ['prepared', 'published', 'history'])
@pytest.mark.parametrize('rejected', [False, True])
def test_native_checkpoint_publication_is_recoverable(tmp_path, monkeypatch, boundary, rejected):
    from proteus.core import episode, snapshot, EvaluatorSpec, EvalResult, GoalConfig
    from test_goals import RecordingHarness, _cfg
    evaluations = []
    def grade(trace, context):
        evaluations.append(context.episode)
        return EvalResult('score', 1.0 if context.episode == 0 else 0.5)
    goal = GoalConfig.of(evaluators=(EvaluatorSpec('score', grade, include_initial=rejected),),
                         selection='accept_reject')
    adapter = RecordingHarness()
    cfg = _cfg(tmp_path, adapter, goal, episodes=1)
    publish = snapshot.publish_checkpoint
    write = episode._write_json_atomic
    def interrupted_publish(*args):
        if boundary == 'prepared':
            raise KeyboardInterrupt()
        publish(*args)
        if boundary == 'published':
            raise KeyboardInterrupt()
    def interrupted_write(path, value):
        write(path, value)
        if boundary == 'history' and path == episode.eval_history_path(cfg.root):
            raise KeyboardInterrupt()
    monkeypatch.setattr(snapshot, 'publish_checkpoint', interrupted_publish)
    monkeypatch.setattr(episode, '_write_json_atomic', interrupted_write)
    with pytest.raises(KeyboardInterrupt):
        episode.run(cfg)
    calls = list(evaluations)
    monkeypatch.setattr(snapshot, 'publish_checkpoint', publish)
    monkeypatch.setattr(episode, '_write_json_atomic', write)
    result = episode.run(cfg, start=episode.completed_episodes(cfg), resume=True)
    assert result.episodes_complete == 1 and not result.error
    assert evaluations == calls
    assert result.eval_history[0]['accepted'] is not rejected
    assert len(adapter.prompts) == 1
    assert not (episode.private_record_dir(cfg.root) / 'episode_checkpoint.json').exists()


@pytest.mark.parametrize('failure', [RecoveryRequired, KeyboardInterrupt, SystemExit])
def test_native_uncertainty_does_not_publish_terminal_receipt(tmp_path, failure):
    from proteus.core.native_inputs import native_input, read_native_inputs
    from test_native_inputs import spec
    with pytest.raises(failure):
        with native_input(spec(tmp_path), backend='pi', phase='act', scope='harness_cli', payload=['test']):
            raise failure('unknown external state')
    rows = read_native_inputs(tmp_path)
    assert len(rows) == 1 and rows[0]['outcome'] is None


@pytest.mark.parametrize('failure', [subprocess.TimeoutExpired('docker', 1), KeyboardInterrupt()])
def test_docker_cleanup_is_bounded_and_blocks_until_reconciled(tmp_path, monkeypatch, failure):
    from proteus.sandbox.docker import DockerSandbox, SandboxConfig
    calls = []
    healthy = False
    def run(args, **kwargs):
        calls.append((args, kwargs))
        assert kwargs['timeout'] > 0
        if args[1] == 'run':
            raise failure
        if not healthy:
            raise subprocess.TimeoutExpired(args, kwargs['timeout'])
        return subprocess.CompletedProcess(args, 0, '', '')
    monkeypatch.setattr(subprocess, 'run', run)
    sandbox = DockerSandbox(SandboxConfig())
    with pytest.raises(RecoveryRequired, match='cleanup unverified'):
        sandbox.run(tmp_path, ['true'], {}, timeout_s=1)
    before = len(calls)
    with pytest.raises(RecoveryRequired, match='reconcile_cleanup'):
        DockerSandbox(SandboxConfig()).run(tmp_path, ['true'], {}, timeout_s=1)
    assert len(calls) == before
    assert calls[-1][1]['timeout'] <= sandbox.CLEANUP_SECONDS
    healthy = True
    sandbox.reconcile_cleanup(tmp_path)
    with pytest.raises(type(failure)):
        sandbox.run(tmp_path, ['true'], {}, timeout_s=1)


@pytest.mark.parametrize('mode', ['stage_map', 'workflow_map'])
def test_uncertain_map_actor_stops_new_work_and_resume(tmp_path, mode):
    if mode == 'stage_map':
        from test_v1_parallel import setup, Options
        registry, recipe = setup(options=Options(prompts=('first', 'second'), workers=1), limit=1)
    else:
        from test_v1_workflows import runnable
        registry, recipe = runnable()
        recipe = replace(recipe, budget=replace(recipe.budget, max_concurrency=1))
    calls = []
    class Actor:
        model_call_bound = 1
        def execute(self, spec, request, workspace):
            calls.append(request.prompt)
            raise RecoveryRequired('external operation still running')
    ref = recipe.actors['optimizer'].runtime.plugin
    registry._plugins[(ref.name, ref.version)] = replace(registry.get(ref), factory=lambda opts: Actor())
    root = tmp_path / 'run'
    lock = registry.make_lock(recipe, [SOURCE])
    result = run_recipe(recipe, registry=registry, lock=lock, root=root)
    assert result.status == OperationStatus.FAILED and 'RecoveryRequired' in result.diagnostic
    book = run_ledger(root)
    before = book.events()
    result = run_recipe(recipe, registry=registry, lock=lock, root=root, resume=True)
    assert result.status == OperationStatus.FAILED and 'RecoveryRequired' in result.diagnostic
    assert len(calls) == 1 and book.events() == before
    assert book.balance()['active_slots'] == 1


def test_build_interruption_retains_started_reservation(tmp_path, monkeypatch):
    from proteus.execution import builds
    from test_v1_source_build import services_fixture, fake_docker, execute
    services, candidate, spec = services_fixture(tmp_path)
    fake_docker(monkeypatch)
    def interrupt(*args):
        raise RecoveryRequired('builder cleanup not confirmed')
    monkeypatch.setattr(builds, 'run_build', interrupt)
    with pytest.raises(RecoveryRequired):
        execute(services, candidate, spec, 1)
    assert services.ledger.entries()[0]['state'] == 'started'
    assert not services.builds.records
    assert not list((services.root / 'private/builds/attempts').glob('*/receipt.json'))


def test_docker_polled_cleanup_never_drains_pipes_without_timeout(tmp_path, monkeypatch):
    from proteus.sandbox.docker import DockerSandbox, SandboxConfig
    timeouts = []
    class Process:
        returncode = None
        def communicate(self, *, timeout):
            timeouts.append(timeout)
            if len(timeouts) == 1:
                raise KeyboardInterrupt()
            raise subprocess.TimeoutExpired('pipes', timeout)
        def poll(self):
            return self.returncode
        def kill(self):
            self.returncode = -9
    monkeypatch.setattr(subprocess, 'Popen', lambda *args, **kwargs: Process())
    def remove(args, **kwargs):
        assert kwargs['timeout'] <= DockerSandbox.CLEANUP_SECONDS
        return subprocess.CompletedProcess(args, 0, '', '')
    monkeypatch.setattr(subprocess, 'run', remove)
    with pytest.raises(RecoveryRequired, match='cleanup unverified'):
        DockerSandbox(SandboxConfig()).run(tmp_path, ['true'], {}, timeout_s=1, stop_check=lambda: False)
    assert len(timeouts) == 3 and all(0 < value <= DockerSandbox.CLEANUP_SECONDS for value in timeouts)


@pytest.mark.skipif(not os.environ.get('PROTEUS_HARBOR_PYTHON'), reason='requires live Docker gate')
@pytest.mark.parametrize('polled', [False, True])
def test_live_harbor_sandbox_timeout_removes_actual_container(tmp_path, polled):
    from proteus.sandbox.docker import DockerSandbox, SandboxConfig
    from proteus.examples.harbor_evaluation import IMAGE
    removed = []
    class Sandbox(DockerSandbox):
        def _cleanup(self, name, marker, proc=None):
            removed.append(name)
            return super()._cleanup(name, marker, proc)
    sandbox = Sandbox(SandboxConfig(image=IMAGE))
    with pytest.raises(subprocess.TimeoutExpired):
        sandbox.run(tmp_path, ['python', '-c', 'import time; time.sleep(60)'], {}, timeout_s=1,
                    stop_check=(lambda: False) if polled else None)
    assert len(removed) == 1
    inspect = subprocess.run(['docker', 'container', 'inspect', removed[0]],
                             capture_output=True, text=True, timeout=10)
    assert inspect.returncode != 0 and 'No such' in inspect.stderr


@pytest.mark.skipif(not os.environ.get('PROTEUS_HARBOR_PYTHON'), reason='requires live Docker gate')
def test_live_harbor_sandbox_keyboard_interrupt_removes_actual_container(tmp_path):
    import signal
    import sys
    import time
    import uuid
    from proteus.examples.harbor_evaluation import IMAGE
    suffix = uuid.uuid4().hex[:12]
    name = 'proteus-' + suffix
    program = '''
import sys,uuid
from pathlib import Path
from types import SimpleNamespace
from proteus.sandbox.docker import DockerSandbox,SandboxConfig
uuid.uuid4=lambda:SimpleNamespace(hex=sys.argv[3])
DockerSandbox(SandboxConfig(image=sys.argv[2])).run(Path(sys.argv[1]),
    ['python','-c','import time;time.sleep(60)'],{},timeout_s=60)
'''
    process = subprocess.Popen([sys.executable, '-c', program, str(tmp_path), IMAGE, suffix],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 20
        while True:
            inspect = subprocess.run(['docker', 'container', 'inspect', '--format', '{{.State.Running}}', name],
                                     capture_output=True, text=True, timeout=5)
            if inspect.returncode == 0 and inspect.stdout.strip() == 'true':
                break
            assert process.poll() is None and time.monotonic() < deadline
            time.sleep(0.05)
        process.send_signal(signal.SIGINT)
        _, error = process.communicate(timeout=15)
        assert process.returncode != 0 and 'KeyboardInterrupt' in error
        inspect = subprocess.run(['docker', 'container', 'inspect', name],
                                 capture_output=True, text=True, timeout=10)
        assert inspect.returncode != 0 and 'No such' in inspect.stderr
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
        subprocess.run(['docker', 'rm', '-f', name], capture_output=True, timeout=10)


def test_cleanup_marker_storage_failure_still_reports_uncertainty(tmp_path, monkeypatch):
    from proteus.sandbox.docker import DockerSandbox, SandboxConfig
    def failed_run(args, **kwargs):
        raise subprocess.TimeoutExpired(args, kwargs['timeout'])
    def failed_write(*args, **kwargs):
        raise OSError('disk full')
    monkeypatch.setattr(subprocess, 'run', failed_run)
    monkeypatch.setattr('proteus.core.recovery._write', failed_write)
    with pytest.raises(RecoveryRequired, match='marker could not be persisted'):
        DockerSandbox(SandboxConfig()).run(tmp_path, ['true'], {}, timeout_s=1)
