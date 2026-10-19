"""Guard against falsely accepting a compiler failure or an incomplete repair/resume."""
from copy import deepcopy
import json

import pytest

from check_native_source_docker import adapter_for, marked
from check_native_source_repair import BROKEN, RepairProvider, controller_reports, require_rejected_candidate


def request():
    return {'model': 'fixture', 'stream': True, 'messages': [],
            'tools': [{'function': {'name': name, 'parameters': {'properties': {'path': {}, 'content': {}}}}}
                      for name in ('read', 'write')]}


def fixture(tmp_path):
    source = tmp_path/'entry.ts'
    source.write_text('#!/usr/bin/env node\nconsole.log("original source");\n')
    return RepairProvider(source, str(source))


def execute(provider, body):
    """Drive the fixture protocol; actual native tools run in the Docker integration job."""
    reply, finish = provider.response(body)
    if finish == 'stop':
        return
    call = reply['tool_calls'][0]
    args = json.loads(call['function']['arguments'])
    assert args['path'] == str(provider.source)
    if call['function']['name'] == 'write':
        provider.source.write_text(args['content'])
        content = 'Written'
    else:
        content = provider.source.read_text()
    body['messages'].append({'role': 'tool', 'tool_call_id': call['id'], 'content': content})
    body['messages'].append({'role': 'user', 'content': 'Additional instructions from: candidate/AGENTS.md'})


def test_repair_fixture_reads_the_failed_candidate_and_does_not_repeat_completion(tmp_path):
    provider = fixture(tmp_path)
    initial = provider.source.read_text()
    first = request()
    for _ in range(3):
        execute(provider, first)
    assert provider.source.read_text() == BROKEN
    repair = request()
    for _ in range(3):
        execute(provider, repair)
    assert provider.source.read_text() == marked(initial)
    execute(provider, request())
    assert len(provider.requests) == 7
    with pytest.raises(AssertionError, match='repeated completed episode'):
        provider.response(request())


@pytest.mark.parametrize('failure', ['wrong_candidate', 'wrong_read', 'wrong_id', 'duplicate', 'user_only'])
def test_repair_fixture_refuses_a_fabricated_or_unrelated_repair_read(tmp_path, failure):
    provider = fixture(tmp_path)
    body = request()
    for _ in range(3):
        execute(provider, body)
    body = request()
    if failure == 'wrong_candidate':
        provider.source.write_text(provider.initial)
    else:
        execute(provider, body)
        read = body['messages'][0]
        if failure == 'wrong_read':
            read['content'] = provider.initial
        elif failure == 'wrong_id':
            read['tool_call_id'] = 'call_repair_0'
        elif failure == 'duplicate':
            body['messages'].append(deepcopy(read))
        else:
            read['role'] = 'user'
    with pytest.raises(AssertionError):
        provider.response(body)


def rejected():
    return {'command': ['--version'], 'returncode': 97,
            'stderr': 'self-edited source does not build: TS1005',
            'mounts': [['/tmp/fixture/run/harness', '/workspace']]}


def test_repair_gate_accepts_exactly_one_candidate_compiler_failure():
    require_rejected_candidate([rejected(), {'returncode': 0}])


@pytest.mark.parametrize('failure', ['docker', 'cold_start', 'missing_source', 'wrong_tree', 'wrong_command', 'duplicate', 'none'])
def test_repair_gate_rejects_unrelated_failures(tmp_path, failure):
    call = rejected()
    if failure == 'docker':
        call.update(returncode=125, stderr='Cannot connect to the Docker daemon')
    elif failure == 'cold_start':
        call.update(returncode=98)
    elif failure == 'missing_source':
        call['stderr'] = 'source tree is missing'
    elif failure == 'wrong_tree':
        call['mounts'][0][0] = '/tmp/fixture/broken/harness'
    elif failure == 'wrong_command':
        call['command'] = ['--help']
    calls = [] if failure == 'none' else [call, deepcopy(call)] if failure == 'duplicate' else [call]
    with pytest.raises(AssertionError):
        require_rejected_candidate(calls)


@pytest.mark.parametrize('backend', ['pi', 'dsh'])
def test_reconstructed_adapter_keeps_native_settings_and_previous_docker_calls(tmp_path, backend):
    url = 'http://127.0.0.1:20000/v1'
    adapter_for(backend, 'fixture-image', tmp_path, url)
    files = {path: path.read_bytes() for path in (tmp_path/'run').rglob('*') if path.is_file()}
    call = {'command': ['--version'], 'returncode': 0, 'stdout': 'ok', 'stderr': '', 'mounts': []}
    (tmp_path/'docker-calls.json').write_text(json.dumps([call]))
    _, sandbox = adapter_for(backend, 'fixture-image', tmp_path, url, resume=True)
    assert sandbox.calls == [call]
    assert all(path.read_bytes() == content for path, content in files.items())
    with pytest.raises(AssertionError):
        adapter_for(backend, 'fixture-image', tmp_path, url.replace('20000', '20001'), resume=True)


def saved_reports(root):
    processes = []
    for index, mode in enumerate(('first', 'repair', 'complete')):
        pid = 100 + index
        processes.append({'mode': mode, 'pid': pid, 'returncode': 0})
        report = {'pid': pid, 'start': [0, 1, 3][index],
                  'result': {'episodes_complete': [1, 3, 3][index], 'error': ''}}
        (root/f'controller-{mode}.json').write_text(json.dumps(report))
    (root/'controller-processes.json').write_text(json.dumps(processes))


def test_three_controller_processes_reach_the_requested_checkpoints(tmp_path):
    saved_reports(tmp_path)
    assert len(controller_reports(tmp_path)) == 3


@pytest.mark.parametrize('failure', ['same_process', 'wrong_pid', 'failed_process', 'incomplete', 'error', 'wrong_start'])
def test_saved_repair_proof_refuses_incomplete_or_same_process_runs(tmp_path, failure):
    saved_reports(tmp_path)
    path = tmp_path/'controller-repair.json'
    report = json.loads(path.read_text())
    processes_path = tmp_path/'controller-processes.json'
    processes = json.loads(processes_path.read_text())
    if failure == 'same_process':
        processes[1]['pid'] = report['pid'] = 100
    elif failure == 'wrong_pid':
        report['pid'] = 500
    elif failure == 'failed_process':
        processes[1]['returncode'] = 1
    elif failure == 'incomplete':
        report['result']['episodes_complete'] = 2
    elif failure == 'error':
        report['result']['error'] = 'source failed to activate'
    else:
        report['start'] = 0
    path.write_text(json.dumps(report))
    processes_path.write_text(json.dumps(processes))
    with pytest.raises(AssertionError):
        controller_reports(tmp_path)
