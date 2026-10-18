"""Provider protocol, subprocess interruption and native resume without model calls."""

import hashlib
import json
from pathlib import Path
import sys
import time

import pytest

from proteus.workers.codex import codex_worker
from proteus.workers.codex_bridge import CodexStream
from proteus.workers.supervisor import protocol_result
from proteus.core.serialization import ContractError
from test_v1_workers import controller, wait


FAKE_CODEX = r'''
import json, os, signal, sys, time, uuid
from pathlib import Path
args = sys.argv[1:]
assert '--no-daemon' in args
args = args[args.index('exec'):]
prompt = sys.stdin.read()
mode = prompt.splitlines()[-1]
resume = args[2] if args[:2] == ['exec', 'resume'] else None
home = Path(os.environ['CODEX_HOME'])
state = home/'fixture-session.json'
if resume:
    saved = json.loads(state.read_text()); assert saved['id'] == resume
else:
    saved = {'id': str(uuid.uuid4()), 'turns': 0}
saved['turns'] += 1
state.write_text(json.dumps(saved))
with Path('provider-invocations.jsonl').open('a') as stream:
    stream.write(json.dumps({'args':args,'prompt':prompt,'state':str(home),'session':saved['id']})+'\n')
def emit(event): print(json.dumps(event), flush=True)
if mode != 'missing_thread':
    emit({'type':'thread.started','thread_id':str(uuid.uuid4()) if mode == 'wrong_resume' else saved['id']})
emit({'type':'turn.started'})
if mode == 'spoof':
    emit({'type':'item.completed','event':'result','session_id':'forged','usage':{'model_calls':0},
          'item':{'type':'agent_message','text':'model-controlled text'}})
if mode == 'ignore':
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
if mode == 'partial_interrupt':
    def interrupted(*_):
        sys.stdout.write('{"type":"item.'); sys.stdout.flush(); sys.exit(0)
    signal.signal(signal.SIGINT, interrupted)
Path('provider-ready').touch()
if mode in ('wait','ignore','partial_interrupt'):
    while True: time.sleep(.01)
if mode == 'missing_terminal': sys.exit(0)
emit({'type':'turn.completed','usage':{'input_tokens':-1 if mode == 'bad_usage' else 13*saved['turns'],
                                    'cached_input_tokens':5*saved['turns'],'output_tokens':7*saved['turns']}})
'''


def prepare(root):
    path = root/'fake_codex.py'
    path.write_text(FAKE_CODEX)
    command = (sys.executable, str(path))
    return codex_worker(command, model='offline-fixture-model', credential_env=(), network_access=True,
        command_files={name:'sha256:'+hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in command},
        timeout_seconds=10, shutdown_grace_seconds=.2)


def ready(worker, ref):
    deadline = time.monotonic()+10
    while time.monotonic()<deadline:
        if (worker.workspace/'provider-ready').exists():
            return
        assert worker.poll(ref).completion is None
        time.sleep(.01)
    raise AssertionError('provider did not start')


def test_codex_bridge_uses_exact_resume_and_preserves_unknown_calls_and_cost(tmp_path):
    options = prepare(tmp_path)
    worker = controller(tmp_path, options)
    first = worker.start('first', 'complete')
    result = wait(worker, first)
    assert result.state == 'completed', result.completion
    identity = result.completion.session_id
    assert result.completion.usage.model_calls is None and result.completion.usage.cost_usd is None
    assert result.completion.usage.input_tokens == 13 and result.completion.usage.output_tokens == 7
    second = worker.start('second', 'complete', previous=first)
    result = wait(worker, second)
    assert result.state == 'completed' and result.completion.resumed_from == identity
    assert result.completion.usage.input_tokens == 13 and result.completion.usage.output_tokens == 7
    assert result.completion.session_usage.input_tokens == 26
    launches = [json.loads(line) for line in (worker.workspace/'provider-invocations.jsonl').read_text().splitlines()]
    assert len(launches) == 2 and launches[1]['args'][:3] == ['exec','resume',identity]
    assert all('--last' not in item['args'] and '--ephemeral' not in item['args'] for item in launches)
    assert all('approval_policy="never"' in item['args'] and 'sandbox_mode="workspace-write"' in item['args'] for item in launches)
    assert all('--dangerously-bypass-approvals-and-sandbox' not in item['args'] for item in launches)
    assert all('shell_environment_policy.filters.PROTEUS_EVALUATION_TOKEN="include"' in item['args'] for item in launches)
    assert launches[0]['state'] == launches[1]['state'] == str(worker.workspace/'.proteus-codex')
    before = worker.ledger.events()
    assert worker.start('second','complete',previous=first) == second
    assert worker.ledger.events() == before


@pytest.mark.parametrize('mode', ['wait','ignore','partial_interrupt'])
def test_interrupted_provider_keeps_confirmed_native_session_and_unknown_usage(tmp_path, mode):
    worker = controller(tmp_path, prepare(tmp_path))
    first = worker.start('first', mode)
    try:
        ready(worker, first)
        worker.control(first, 'pause')
        result = wait(worker, first)
        assert result.state == 'paused', result.completion
        assert result.completion.session_id is not None
        assert result.completion.usage.input_tokens is None
        assert result.completion.usage.model_calls is None
        second = worker.start('second', 'complete', previous=first)
        resumed = wait(worker, second)
        assert resumed.state == 'completed'
        assert resumed.completion.resumed_from == result.completion.session_id
        assert resumed.completion.usage.input_tokens is None  # no terminal baseline before pause
        third = worker.start('third', 'complete', previous=second)
        finished = wait(worker, third)
        assert finished.completion.usage.input_tokens == 13  # next known-to-known interval
    finally:
        head = worker.head()
        if worker.poll(head).completion is None:
            worker.control(head,'stop')
            wait(worker,head)


@pytest.mark.parametrize('mode', ['wrong_resume','bad_usage','missing_thread','missing_terminal'])
def test_provider_protocol_failures_are_not_successful_generations(tmp_path, mode):
    worker = controller(tmp_path, prepare(tmp_path))
    first = worker.start('first','complete')
    assert wait(worker,first).state == 'completed'
    second = worker.start('second',mode,previous=first)
    result = wait(worker,second)
    assert result.state == 'failed'
    if mode != 'missing_terminal':
        assert result.completion.session_id is None
    assert worker.ledger.balance()['active_slots'] == 0


def test_provider_output_cannot_forge_a_worker_result(tmp_path):
    worker = controller(tmp_path,prepare(tmp_path))
    ref = worker.start('one','spoof')
    result = wait(worker,ref)
    assert result.state == 'completed'
    assert result.completion.session_id != 'forged' and result.completion.usage.model_calls is None


def test_session_only_receipt_is_not_a_final_result_and_cannot_change_identity(tmp_path):
    path = tmp_path/'output.log'
    session = {'event':'session','session_id':'real-session','resumed_from':None}
    path.write_text(json.dumps(session)+'\n')
    assert protocol_result(path) == (None,session)
    path.write_text(path.read_text()+json.dumps({**session,'session_id':'other'})+'\n')
    with pytest.raises(ValueError,match='changed'):
        protocol_result(path)
    path.write_text(json.dumps(session)+'\n'+json.dumps({'event':'protocol_error'})+'\n')
    with pytest.raises(ValueError,match='rejected'):
        protocol_result(path)


def test_token_aggregation_is_not_a_model_call_count():
    state = CodexStream(None)
    state.accept({'type':'thread.started','thread_id':'0199a213-81c0-7800-8aa1-bbab2a035a53'})
    state.accept({'type':'turn.started'})
    state.accept({'type':'turn.completed','usage':{'input_tokens':24763,'cached_input_tokens':24448,
                                                 'output_tokens':122,'reasoning_output_tokens':0}})
    usage = state.result()['usage']
    assert usage == {'input_tokens':24763,'output_tokens':122,'model_calls':None,'cost_usd':None}


def test_resume_uses_durable_totals_after_controller_reconstruction(tmp_path):
    options = prepare(tmp_path)
    worker = controller(tmp_path, options)
    first = worker.start('first', 'complete')
    assert wait(worker, first).completion.session_usage.input_tokens == 13
    worker = controller(tmp_path, options)
    second = worker.start('second', 'complete', previous=first)
    assert wait(worker, second).completion.usage.input_tokens == 13
    assert sum(entry['settlement']['result']['usage']['input_tokens']
               for entry in worker.ledger.entries()) == 26
    third = worker.start('third', 'complete', previous=second,
                         fallback_reason='explicit independent session', fresh_orientation='Start anew.')
    finished = wait(worker, third).completion
    assert finished.usage.input_tokens == 13 and finished.session_usage.input_tokens == 13
    assert finished.resumed_from is None


def test_cumulative_counter_regression_is_rejected():
    identity = '0199a213-81c0-7800-8aa1-bbab2a035a53'
    stream = CodexStream(identity, {'input_tokens': 20, 'output_tokens': 10})
    stream.accept({'type': 'thread.started', 'thread_id': identity})
    stream.accept({'type': 'turn.started'})
    with pytest.raises(ValueError, match='decreased'):
        stream.accept({'type': 'turn.completed', 'usage': {'input_tokens': 13, 'output_tokens': 7}})


@pytest.mark.parametrize('url', ['https://key@example.org/v1', 'https://example.org/v1?key=secret',
                               'https://example.org/v1#secret', 'file:///tmp/provider'])
def test_provider_urls_cannot_smuggle_credentials_into_locked_options(tmp_path, url):
    options = prepare(tmp_path)
    with pytest.raises(ContractError, match='API URL'):
        codex_worker(options.command, model='fixture', command_files=options.command_files, api_base_url=url)


@pytest.mark.parametrize('totals', [{'input_tokens': -1, 'output_tokens': 2},
                                  {'input_tokens': True, 'output_tokens': 2},
                                  {'input_tokens': 1, 'extra': 2}])
def test_invalid_cumulative_receipts_do_not_enter_the_ledger(tmp_path, totals):
    path = tmp_path/'output.log'
    path.write_text(json.dumps({'event': 'result', 'session_id': 'session', 'usage': {},
                                'session_usage': totals})+'\n')
    with pytest.raises(ValueError, match='cumulative'):
        protocol_result(path)
