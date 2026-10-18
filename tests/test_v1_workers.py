"""Real subprocess lifecycle, controller exits, native continuity and budgets."""

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

import pytest

from proteus.core.contracts import BudgetLimits
from proteus.core.ledger import Ledger
from proteus.core.serialization import ContractError, content_hash
from proteus.workers import CommandWorkerOptions, WorkerController, WorkerIdentity
from proteus.workers.runtime import write_json


BRIDGE = r'''
import json, signal, sys, time
from pathlib import Path
request = json.loads(sys.stdin.readline())
mode = request['prompt'].splitlines()[-1]
with Path('launches.jsonl').open('a') as f:
    f.write(json.dumps(request) + '\n')
state = Path('session.json')
if request['session_id']:
    saved = json.loads(state.read_text())
    assert saved['id'] == request['session_id']
else:
    saved = {'id': 'session-' + request['operation_id'], 'turns': 0}
saved['turns'] += 1
state.write_text(json.dumps(saved))
done = False
def pause(*_):
    global done
    done = True
signal.signal(signal.SIGINT, pause)
Path('ready-' + request['operation_id']).touch()
if mode == 'wait':
    while not done: time.sleep(0.01)
if mode == 'crash': sys.exit(3)
print(json.dumps({'event': 'result', 'session_id': None if mode == 'no_session' else saved['id'],
    'resumed_from': None if mode == 'wrong_resume' else request['session_id'],
    'usage': {} if mode == 'unknown' else {'model_calls': 1, 'input_tokens': 2, 'output_tokens': 3}}), flush=True)
'''


def prepare(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / 'bridge.py').write_text(BRIDGE)
    return CommandWorkerOptions(command=(sys.executable, str(root / 'bridge.py')),
                                command_files={path: 'sha256:' + hashlib.sha256(Path(path).read_bytes()).hexdigest()
                                               for path in (sys.executable, str(root / 'bridge.py'))},
                                timeout_seconds=10, shutdown_grace_seconds=0.1)


def controller(root, options, *, worker='worker-a', concurrency=2, limits=None):
    identity = WorkerIdentity(run_id='test', config_hash=content_hash('recipe'), lock_hash=content_hash('lock'),
                              worker_id=worker, role='researcher')
    ledger = Ledger(root / 'private/ledger.sqlite3', run_id=identity.run_id,
                    config_hash=identity.config_hash, lock_hash=identity.lock_hash)
    return WorkerController(root, identity=identity, options=options, ledger=ledger,
                            scopes={'run': limits or BudgetLimits(max_concurrency=concurrency)})


def wait(worker, ref, *, ready=False):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        observed = worker.poll(ref)
        if ready and (worker.workspace / ('ready-' + ref.operation_id)).exists():
            return observed
        if observed.completion is not None:
            return observed
        time.sleep(0.025)
    worker.control(ref, 'stop')
    pytest.fail('worker did not reach expected state')


def launches(worker):
    return [json.loads(line) for line in (worker.workspace / 'launches.jsonl').read_text().splitlines()]


def test_completed_generations_resume_native_session_and_replay_without_new_work(tmp_path):
    options = prepare(tmp_path)
    worker = controller(tmp_path, options)
    first = worker.start('first', 'finish')
    assert wait(worker, first).state == 'completed'
    second = worker.start('second', 'finish', previous=first)
    result = wait(worker, second)
    assert result.state == 'completed'
    assert result.completion.resumed_from == 'session-first'
    assert json.loads((worker.workspace / 'session.json').read_text())['turns'] == 2
    events = worker.ledger.events()
    assert worker.start('first', 'finish') == first
    assert worker.head() == second
    assert worker.start('second', 'finish', previous=first) == second
    assert worker.ledger.events() == events
    assert len(launches(worker)) == 2
    assert worker.ledger.balance()['charged']['model_calls'] == 2
    assert worker.ledger.balance()['active_slots'] == 0
    with pytest.raises(ContractError, match='identity changed'):
        worker.start('second', 'different', previous=first)


def test_pause_stop_and_native_session_survive_controller_reconstruction(tmp_path):
    options = prepare(tmp_path)
    worker = controller(tmp_path, options)
    first = worker.start('first', 'wait')
    assert wait(worker, first, ready=True).state == 'active'
    worker = controller(tmp_path, options)
    worker.control(first, 'pause')
    paused = wait(worker, first)
    assert paused.state == 'paused' and paused.completion.session_id == 'session-first'
    second = worker.start('second', 'wait', previous=first)
    wait(worker, second, ready=True)
    with pytest.raises(ContractError, match='current worker generation'):
        worker.control(first, 'stop')
    worker.control(second, 'stop')
    assert wait(worker, second).state == 'stopped'
    assert worker.ledger.balance()['active_slots'] == 0


def test_missing_native_session_requires_explicit_recorded_fresh_orientation(tmp_path):
    worker = controller(tmp_path, prepare(tmp_path))
    first = worker.start('first', 'no_session')
    assert wait(worker, first).state == 'completed'
    with pytest.raises(ContractError, match='explicit fresh fallback'):
        worker.start('second', 'finish', previous=first)
    with pytest.raises(ContractError, match='orientation prompt'):
        worker.start('second', 'finish', previous=first, fallback_reason='session unavailable')
    second = worker.start('second', 'finish', previous=first, fallback_reason='session unavailable',
                          fresh_orientation='Read the persisted work before continuing.')
    result = wait(worker, second)
    assert result.state == 'completed' and result.completion.resumed_from is None
    request = launches(worker)[1]
    assert request['continuity'] == 'fresh_fallback'
    assert request['fallback_reason'] == 'session unavailable'
    assert request['prompt'].startswith('Read the persisted work')


def test_runtime_cannot_silently_replace_native_resume_with_a_fresh_session(tmp_path):
    worker = controller(tmp_path, prepare(tmp_path))
    first = worker.start('first', 'finish')
    wait(worker, first)
    second = worker.start('second', 'wrong_resume', previous=first)
    result = wait(worker, second)
    assert result.state == 'failed'
    assert 'native session' in result.completion.diagnostic
    assert worker.ledger.balance()['charged']['model_calls'] == 2


def test_multiple_workers_share_budget_without_sharing_workspaces(tmp_path):
    options = prepare(tmp_path)
    first, second = (controller(tmp_path, options, worker=name, concurrency=1) for name in ('a', 'b'))
    active = first.start('first', 'wait')
    wait(first, active, ready=True)
    try:
        with pytest.raises(ContractError, match='concurrency budget exhausted'):
            second.start('first', 'finish')
        assert not (second.workspace / 'launches.jsonl').exists()
    finally:
        first.control(active, 'stop')
        wait(first, active)
    ref = second.start('first', 'finish')
    assert wait(second, ref).state == 'completed'
    assert first.workspace != second.workspace


def test_unreported_usage_is_unknown_and_worker_timeout_is_recorded(tmp_path):
    options = replace(prepare(tmp_path), timeout_seconds=0.5)
    worker = controller(tmp_path, options)
    first = worker.start('first', 'unknown')
    assert wait(worker, first).completion.usage.model_calls is None
    second = worker.start('second', 'wait', previous=first)
    result = wait(worker, second)
    assert result.state == 'failed' and 'timeout' in result.completion.diagnostic
    assert worker.ledger.balance()['unknown']['model_calls'] == 1


@pytest.mark.parametrize('limits', [BudgetLimits(max_model_calls=3), BudgetLimits(max_cost_usd=1),
                                   BudgetLimits(max_wall_seconds=10)])
def test_command_runtime_does_not_accept_unenforceable_finite_caps(tmp_path, limits):
    with pytest.raises(ContractError, match='cannot enforce finite'):
        controller(tmp_path, prepare(tmp_path), limits=limits)


CRASHING_CONTROLLER = r'''
import os, sys
from pathlib import Path
from test_v1_workers import controller
from proteus.workers.records import CommandWorkerOptions
from proteus.workers.runtime import CommandWorkerRuntime
root, boundary = Path(sys.argv[1]), sys.argv[2]
options = CommandWorkerOptions.from_json((root/'options.json').read_text())
original = CommandWorkerRuntime.start
def start(self, *args):
    if boundary == 'before': os._exit(86)
    original(self, *args)
    if boundary == 'after': os._exit(86)
CommandWorkerRuntime.start = start
worker = controller(root, options)
worker.start('first', 'wait')
'''


@pytest.mark.parametrize('boundary', ['before', 'after'])
def test_hard_exit_at_dispatch_boundary_reconnects_without_duplicate_work(tmp_path, boundary):
    import os
    options = prepare(tmp_path)
    (tmp_path / 'options.json').write_text(options.to_json())
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(Path(__file__).parent), str(Path(__file__).parents[1])]))
    result = subprocess.run([sys.executable, '-c', CRASHING_CONTROLLER, str(tmp_path), boundary],
                            env=env, capture_output=True, timeout=15)
    assert result.returncode == 86, result.stderr.decode()
    worker = controller(tmp_path, options)
    assert worker.ledger.balance()['active_slots'] == 1
    first = worker.start('first', 'wait')
    wait(worker, first, ready=True)
    worker.control(first, 'pause')
    assert wait(worker, first).state == 'paused'
    assert len(launches(worker)) == 1
    assert len(worker.ledger.entries()) == 1


def test_missing_completion_is_uncertain_and_keeps_budget_reserved(tmp_path, monkeypatch):
    worker = controller(tmp_path, prepare(tmp_path), concurrency=1)
    def dispatch(launch, directory):
        write_json(directory / 'dispatch.json', {'request_hash': content_hash(launch)})
    monkeypatch.setattr(worker.runtime, 'start', dispatch)
    ref = worker.start('first', 'finish')
    assert worker.poll(ref).state == 'uncertain'
    assert worker.start('first', 'finish') == ref
    assert worker.ledger.balance()['active_slots'] == 1
    with pytest.raises(ContractError, match='no terminal receipt'):
        worker.start('second', 'finish', previous=ref)
    assert not (worker.workspace / 'launches.jsonl').exists()


def test_changed_completed_output_fails_closed(tmp_path):
    worker = controller(tmp_path, prepare(tmp_path))
    ref = worker.start('first', 'finish')
    wait(worker, ref)
    (worker._directory('first') / 'stdout.log').write_text('changed')
    with pytest.raises(ContractError, match='output changed'):
        worker.poll(ref)


def test_superseded_request_that_never_obtained_budget_cannot_launch_later(tmp_path):
    options = prepare(tmp_path)
    blocker = controller(tmp_path, options, worker='blocker', concurrency=1)
    worker = controller(tmp_path, options, concurrency=1)
    active = blocker.start('blocking', 'wait')
    wait(blocker, active, ready=True)
    try:
        with pytest.raises(ContractError, match='budget exhausted'):
            worker.start('abandoned', 'finish')
    finally:
        blocker.control(active, 'stop')
        wait(blocker, active)
    newer = worker.start('newer', 'wait')
    wait(worker, newer, ready=True)
    try:
        with pytest.raises(ContractError, match='superseded'):
            worker.start('abandoned', 'finish')
        assert worker.head() == newer
        assert len(launches(worker)) == 1
    finally:
        worker.control(newer, 'stop')
        wait(worker, newer)


def test_two_controllers_race_one_dispatch_without_two_native_sessions(tmp_path):
    import os
    options = prepare(tmp_path)
    (tmp_path / 'options.json').write_text(options.to_json())
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(Path(__file__).parent), str(Path(__file__).parents[1])]))
    processes = [subprocess.Popen([sys.executable, '-c', CRASHING_CONTROLLER, str(tmp_path), 'race'],
                                  env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
    for process in processes:
        out, error = process.communicate(timeout=15)
        assert process.returncode == 0, out + error
    worker = controller(tmp_path, options)
    ref = worker.head()
    wait(worker, ref, ready=True)
    worker.control(ref, 'stop')
    assert wait(worker, ref).state == 'stopped'
    assert len(launches(worker)) == 1 and len(worker.ledger.entries()) == 1


def test_changed_command_bytes_are_rejected_before_runtime_dispatch(tmp_path):
    options = prepare(tmp_path)
    worker = controller(tmp_path, options)
    (tmp_path / 'bridge.py').write_text('raise SystemExit(42)')
    with pytest.raises(ContractError, match='command file changed'):
        worker.start('first', 'finish')
    assert not (worker._directory('first') / 'dispatch.json').exists()


def test_timeout_still_applies_when_bridge_never_reads_large_prompt(tmp_path):
    options = prepare(tmp_path)
    options = replace(options, command=(sys.executable, '-c',
        'import signal, time; signal.signal(signal.SIGINT, signal.SIG_IGN); time.sleep(30)'),
        command_files={sys.executable: options.command_files[sys.executable]}, timeout_seconds=0.2)
    worker = controller(tmp_path, options)
    ref = worker.start('first', 'x' * 1_000_000)
    result = wait(worker, ref)
    assert result.state == 'failed' and 'timeout' in result.completion.diagnostic
    assert result.completion.usage.model_calls is None
    assert worker.ledger.balance()['active_slots'] == 0
