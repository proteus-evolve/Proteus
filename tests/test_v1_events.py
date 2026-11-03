"""Formal observations, persisted policy state and real worker interventions."""

from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from proteus.core.contracts import DataRef
from proteus.core.methods import RunServices
from proteus.core.serialization import ContractError, content_hash
from proteus.events.policies import CadenceOptions, CadencePolicy, CadenceRule
from proteus.events.records import ClockSample, EventObservation
from proteus.examples.event_interventions import prepare, register
from proteus.examples.worker_lifecycle import wait
from proteus.methods.coral.policy import CoralOptions, CoralPolicy
from proteus.recipes import load_recipe
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.runner import run_recipe
from test_v1_evaluation import SOURCE
from test_v1_ledger import run_ledger


def observation(index, *, worker='a', score=1, kind='evaluation', clock=None, invocation='one', sample=True):
    digest = content_hash(index)
    return EventObservation(source_key=digest, worker_id=worker, kind=kind,
        clock=ClockSample(invocation=invocation, monotonic_seconds=index if clock is None else clock),
        result=DataRef(kind='proteus.gateway.result@1', id=digest, content_hash=digest) if kind == 'evaluation' else None,
        score=score if kind == 'evaluation' and sample else None, plateau_sample=sample and kind == 'evaluation')


def rule(name, trigger, every, **kwargs):
    return CadenceRule(name=name, trigger=trigger, every=every, prompt='Research: ' + name, **kwargs)


def test_count_time_and_stall_use_separate_worker_and_global_state():
    policy = CadencePolicy(CadenceOptions(rules=(rule('reflect', 'evaluations', 2),
        rule('consolidate', 'evaluations', 3, scope='global'), rule('pivot', 'plateau', 2), rule('timer', 'time', 10))))
    state = policy.initial_state()
    emitted = []
    for event in (observation(0, score=None), observation(1, score=1), observation(2, worker='b', score=4),
                  observation(3, score=1), observation(4, score=None), observation(5, kind='tick', clock=10)):
        transition = policy.advance(event, state)
        state = transition.state
        emitted.append(tuple(action.name for action in transition.actions))
    assert emitted == [(), ('reflect',), ('consolidate',), (), ('reflect', 'pivot'), ('timer',)]
    assert state.evaluations == 5 and state.workers['a'].evaluations == 4 and state.workers['b'].evaluations == 1
    assert policy.advance(observation(6, kind='tick', clock=11), state).actions == ()


def test_time_after_restart_keeps_unknown_gap_and_never_counts_downtime():
    policy = CadencePolicy(CadenceOptions(rules=(rule('timer', 'time', 10, scope='global'),)))
    state = policy.initial_state()
    for event in (observation(1, kind='tick', clock=10), observation(2, kind='tick', clock=16),
                  observation(3, kind='tick', clock=1000, invocation='restarted')):
        transition = policy.advance(event, state)
        assert not transition.actions
        state = transition.state
    assert state.elapsed.seconds == 6 and state.elapsed.gaps == 1
    assert [a.name for a in policy.advance(observation(4, worker='b', kind='tick', clock=1004,
                                                      invocation='restarted'), state).actions] == ['timer']
    with pytest.raises(ContractError, match='backwards'):
        policy.advance(observation(5, kind='tick', clock=999, invocation='restarted'), state)


@pytest.mark.parametrize('minimize', [False, True])
def test_plateau_epsilon_and_persisted_cooldown(minimize):
    policy = CadencePolicy(CadenceOptions(rules=(rule('pivot', 'plateau', 2, epsilon=0.1),), minimize=minimize))
    state, fired = policy.initial_state(), []
    values = [1, 1.01, 1.02, 1.03, 1.04, 1.2, None, 1.21]
    for index, score in enumerate(values):
        score = -score if minimize and score is not None else score
        transition = policy.advance(observation(index, score=score), state)
        state = type(transition.state).from_json(transition.state.to_json())
        if transition.actions:
            fired.append(index)
    assert fired == [2, 4, 7]


def test_reference_manager_and_persistent_cooldown_are_different_profiles():
    sequences = []
    for profile in ('reference_manager', 'persistent_cooldown'):
        policy = CoralPolicy(CoralOptions(rules=(rule('pivot', 'plateau', 2),), profile=profile))
        state, fired = policy.initial_state(), []
        for index in range(6):
            transition = policy.advance(observation(index), state)
            state = transition.state
            if transition.actions:
                fired.append(index)
        sequences.append(fired)
    assert sequences == [[2, 3, 4, 5], [2, 4]]
    reference = CoralPolicy(CoralOptions(rules=(rule('pivot', 'plateau', 2),)))
    state = reference.advance(observation(0, score=None), reference.initial_state()).state
    assert reference.advance(observation(1, score=None), state).actions  # Actual source counts leading None.
    generic = CadencePolicy(CadenceOptions(rules=(rule('pivot', 'plateau', 2),)))
    state = generic.advance(observation(0, score=None), generic.initial_state()).state
    assert not generic.advance(observation(1, score=None), state).actions


def setup(root):
    root.mkdir(parents=True, exist_ok=True)
    recipe = load_recipe(prepare(root / 'study'))
    registry = builtin_registry()
    register(registry)
    recipe = registry.resolve(recipe)
    lock = registry.make_lock(recipe, [SOURCE])
    (root/'recipe.json').write_text(recipe.to_json())
    (root/'lock.json').write_text(lock.to_json())
    return recipe, registry, lock


def services(root, recipe, registry, lock):
    return RunServices(run_id='run', recipe=recipe, registry=registry, root=root/'run', lock_hash=content_hash(lock))


def cleanup(current):
    for spec in current.recipe.workers:
        worker = current.worker(spec.id)
        head = worker.controller.head()
        if head is not None and worker.poll(head).completion is None:
            worker.controller.control(head, 'stop')
            wait(worker, head)


def test_two_real_workers_get_formal_scores_and_native_interventions_without_repeat(tmp_path):
    recipe, registry, lock = setup(tmp_path)
    current = services(tmp_path, recipe, registry, lock)
    try:
        result = run_recipe(recipe, registry=registry, lock=lock, root=tmp_path/'run',
                            artifact_source=tmp_path/'study/artifact-store')
        assert result.status.value == 'completed', result.diagnostic
        execution = json.loads((tmp_path/'run/execution-0001.json').read_text())
        current.values.restore(execution['values'])
        decisions = [value for ref, value in current.values._values.values() if ref.kind == 'proteus.event.decision@1']
        assert sorted(value.observation.score for value in decisions) == [0, 1]
        assert len(execution['rollouts']) == 5  # private test never enters event history
        ledger = run_ledger(tmp_path/'run')
        old = ledger.events()
        for name in ('a', 'b'):
            worker = current.worker(name)
            launches = [json.loads(line) for line in (worker.controller.workspace/'launches.jsonl').read_text().splitlines()]
            assert len(launches) == 2 and launches[1]['continuity'] == 'native'
            assert launches[1]['session_id'] == 'session-' + name
            assert '[reflect]' in (worker.controller.workspace/'intervention.txt').read_text()
        events = current.events('heartbeat')
        for ref, value in list(current.values._values.values()):
            if ref.kind == 'proteus.event.decision@1':
                assert events.observe('duplicate-' + value.observation.worker_id, value.observation.result) == ref
                assert len(events.apply(ref)) == 1
                result_value = current.values.get(value.observation.result)
                forged = replace(result_value, status='failed', evidence=None, shared_attempt=None, diagnostic='invented')
                forged_ref = current.values.publish('proteus.gateway.result@1', forged)
                with pytest.raises(ContractError, match='formal gateway receipt'):
                    events.observe('forged-' + value.observation.worker_id, forged_ref)
        assert run_recipe(recipe, registry=registry, lock=lock, root=tmp_path/'run', resume=True) == result
        assert ledger.events() == old and ledger.balance()['active_slots'] == 0
    finally:
        cleanup(current)


def test_event_operation_binding_and_restored_clock_are_checked(tmp_path):
    recipe, registry, lock = setup(tmp_path)
    current = services(tmp_path, recipe, registry, lock)
    events = current.events('heartbeat')
    first = events.tick('one', 'a')
    assert events.tick('one', 'a') == first
    with pytest.raises(ContractError):
        events.tick('one', 'b')
    with pytest.raises(ContractError, match='outside'):
        events.tick('other', 'foreign')
    from proteus.events.service import validate_restored
    validate_restored(current)
    value = current.values.get(first)
    events._path('observations', value.observation.source_key).unlink()
    with pytest.raises(OSError):
        validate_restored(current)


def test_event_configuration_and_plugin_are_locked_before_execution(tmp_path):
    recipe, registry, lock = setup(tmp_path)
    with pytest.raises(ContractError, match='unknown gateway'):
        replace(recipe, event_policies=(replace(recipe.event_policies[0], gateway='foreign'),))
    with pytest.raises(ContractError, match='orientation'):
        replace(recipe.event_policies[0], resume_policy='fresh')
    spec = recipe.event_policies[0]
    changed = replace(recipe, event_policies=(replace(spec, operator=replace(spec.operator,
        options={**spec.operator.options, 'minimize': True})),))
    assert changed.config_hash != recipe.config_hash
    assert any(item.plugin.name == 'proteus.event.cadence' for item in lock.plugins)


def test_plugin_transition_rejects_untyped_actions_and_foreign_workers(tmp_path):
    from proteus.events.records import EventAction, Transition
    recipe, registry, lock = setup(tmp_path)
    current = services(tmp_path, recipe, registry, lock)
    event = current.events('heartbeat')
    event.policy.advance = lambda observation, state: Transition(state, [])
    with pytest.raises(ContractError, match='tuple of typed'):
        event.tick('untyped', 'a')
    event.policy.advance = lambda observation, state: Transition(state, (EventAction(
        name='escape', worker_id='foreign', prompt='invalid target'),))
    with pytest.raises(ContractError, match='undeclared worker'):
        event.tick('foreign', 'a')
    assert not event._decisions()


CRASH = r'''
import os, sys
from pathlib import Path
from proteus.events import service as module
from proteus.workers.service import WorkerService
from proteus.recipes.manifest import DependencyLock
from test_v1_events import load_recipe, builtin_registry, register, run_recipe
root, boundary = Path(sys.argv[1]), sys.argv[2]
recipe = load_recipe(root/'recipe.json')
lock = DependencyLock.from_json((root/'lock.json').read_text())
registry = builtin_registry(); register(registry)
write, start, control = module._write, WorkerService.start, WorkerService.control
def write_crash(path, value):
    write(path, value)
    if path.parent.name == boundary: os._exit(86)
module._write = write_crash
def start_crash(self, operation_id, *args, **kwargs):
    value = start(self, operation_id, *args, **kwargs)
    if boundary == 'worker_start' and operation_id.startswith('event:'): os._exit(86)
    return value
WorkerService.start = start_crash
def control_crash(self, *args, **kwargs):
    value = control(self, *args, **kwargs)
    if boundary == 'worker_pause': os._exit(86)
    return value
WorkerService.control = control_crash
result=run_recipe(recipe, registry=registry, lock=lock, root=root/'run', artifact_source=root/'study/artifact-store')
raise RuntimeError(result.diagnostic)
'''


@pytest.mark.parametrize('boundary', ['operations', 'observations', 'plans', 'worker_pause', 'worker_start', 'dispatches'])
def test_hard_exit_reuses_observation_plan_and_worker_generation(tmp_path, boundary):
    recipe, registry, lock = setup(tmp_path)
    current = services(tmp_path, recipe, registry, lock)
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(Path(__file__).parent), str(Path(__file__).parents[1])]))
    try:
        process = subprocess.run([sys.executable, '-c', CRASH, str(tmp_path), boundary], env=env,
                                 capture_output=True, timeout=45)
        assert process.returncode == 86, process.stdout.decode() + process.stderr.decode()
        result = run_recipe(recipe, registry=registry, lock=lock, root=tmp_path/'run', resume=True)
        assert result.status.value == 'completed', result.diagnostic
        for name in ('a', 'b'):
            worker = current.worker(name)
            assert len((worker.controller.workspace/'launches.jsonl').read_text().splitlines()) == 2
        execution = json.loads(sorted((tmp_path/'run').glob('execution-*.json'))[-1].read_text())
        decisions = [item['value'] for item in execution['values'] if item['ref']['kind'] == 'proteus.event.decision@1']
        assert len(decisions) == 2 and decisions[-1]['payload']['evaluations'] == 2
        ledger = run_ledger(tmp_path/'run')
        entries = ledger.entries()
        assert len([item for item in entries if item['identity']['metadata']['kind'] == 'worker']) == 4
        assert ledger.balance()['active_slots'] == 0
        old = ledger.events()
        assert run_recipe(recipe, registry=registry, lock=lock, root=tmp_path/'run', resume=True) == result
        assert ledger.events() == old
    finally:
        cleanup(current)


def test_pause_during_slow_gateway_poll_preserves_native_session(tmp_path):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading

    polling, release = threading.Event(), threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            payload = b'{"ticket_id":"accepted"}'
            self.send_response(200)
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            # Hold the response beyond the client's timeout. A pause must stop
            # retries and confirm the session before supervisor escalation.
            polling.set()
            release.wait(5)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    recipe, registry, lock = setup(tmp_path)
    current = services(tmp_path, recipe, registry, lock)
    worker = current.worker('a')
    worker.controller.environment = {'PROTEUS_EVALUATION_URL': f'http://127.0.0.1:{server.server_port}',
                                     'PROTEUS_EVALUATION_TOKEN': 'fixture-token'}
    try:
        first = worker.start('blocked-poll', 'Submit and await feedback.')
        assert polling.wait(5), 'worker never reached its pending HTTP request'
        worker.control(first, 'pause')
        paused = wait(worker, first)
        assert paused.state == 'paused' and paused.completion.session_id == 'session-a', paused
        second = worker.start('continue', 'Continue after the pause.', previous=first)
        completed = wait(worker, second)
        assert completed.state == 'completed' and completed.completion.resumed_from == 'session-a'
        assert current.ledger.balance()['active_slots'] == 0
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        cleanup(current)


def test_fresh_intervention_is_an_explicit_recorded_policy(tmp_path):
    recipe, registry, _ = setup(tmp_path)
    recipe = replace(recipe, event_policies=(replace(recipe.event_policies[0], resume_policy='fresh',
        fresh_orientation='Read the saved workspace and formal feedback before continuing.'),))
    lock = registry.make_lock(recipe, [SOURCE])
    current = services(tmp_path, recipe, registry, lock)
    try:
        result = run_recipe(recipe, registry=registry, lock=lock, root=tmp_path/'run',
                            artifact_source=tmp_path/'study/artifact-store')
        assert result.status.value == 'completed', result.diagnostic
        for name in ('a', 'b'):
            worker = current.worker(name)
            launches = [json.loads(line) for line in (worker.controller.workspace/'launches.jsonl').read_text().splitlines()]
            assert launches[1]['continuity'] == 'fresh_fallback' and launches[1]['session_id'] is None
            assert launches[1]['fallback_reason'] == 'event policy explicitly requests a fresh generation'
            assert launches[1]['prompt'].startswith('Read the saved workspace')
    finally:
        cleanup(current)


def test_unconfirmed_pause_keeps_method_recoverable_and_does_not_dispatch_again(tmp_path, monkeypatch):
    from proteus.workers.records import WorkerObservation
    from proteus.workers.service import WorkerService
    recipe, registry, _ = setup(tmp_path)
    recipe = replace(recipe, event_policies=(replace(recipe.event_policies[0], pause_timeout_seconds=1),))
    lock = registry.make_lock(recipe, [SOURCE])
    current = services(tmp_path, recipe, registry, lock)
    actual_poll, actual_control, paused = WorkerService.poll, WorkerService.control, set()

    def control(self, ref, action):
        value = actual_control(self, ref, action)
        if action == 'pause':
            paused.add(ref)
        return value

    def poll(self, ref):
        return WorkerObservation(ref=ref, state='uncertain') if ref in paused else actual_poll(self, ref)

    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(WorkerService, 'control', control)
            scoped.setattr(WorkerService, 'poll', poll)
            result = run_recipe(recipe, registry=registry, lock=lock, root=tmp_path/'run',
                                artifact_source=tmp_path/'study/artifact-store')
            assert result.status.value == 'failed' and 'RecoveryRequired' in result.diagnostic
            assert all(len((current.worker(name).controller.workspace/'launches.jsonl').read_text().splitlines()) == 1
                       for name in ('a', 'b'))
        recovered = run_recipe(recipe, registry=registry, lock=lock, root=tmp_path/'run', resume=True)
        assert recovered.status.value == 'completed', recovered.diagnostic
        assert all(len((current.worker(name).controller.workspace/'launches.jsonl').read_text().splitlines()) == 2
                   for name in ('a', 'b'))
        assert run_ledger(tmp_path/'run').balance()['active_slots'] == 0
    finally:
        cleanup(current)
