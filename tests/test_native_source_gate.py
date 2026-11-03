"""Infrastructure failures cannot satisfy the real-source compiler gate."""
from copy import deepcopy

import pytest

from check_native_source_docker import Provider, require_compiler_rejection


def compiler_error():
    return {'command': ['--version'], 'returncode': 97,
            'stderr': 'self-edited source does not build; tail of the build log:\nTS1005 syntax error',
            'mounts': [('/tmp/study/broken/harness', '/workspace'), ('/tmp/study/broken/.pi-state', '/state')]}


def test_recognizes_the_source_wrapper_compiler_failure():
    require_compiler_rejection([compiler_error()])


@pytest.mark.parametrize('failure', ['docker', 'missing_source', 'cold_start', 'wrong_tree', 'duplicate'])
def test_refuses_other_failed_probes(failure):
    call = compiler_error()
    if failure == 'docker':
        call.update(returncode=125, stderr='Cannot connect to the Docker daemon')
    elif failure == 'missing_source':
        call.update(stderr='source package tree is missing; refusing baked fallback')
    elif failure == 'cold_start':
        call.update(returncode=98, stderr='self-edited source fails a fresh headless-profile cold start')
    elif failure == 'wrong_tree':
        call['mounts'] = [('/tmp/study/run/harness', '/workspace')]
    calls = [call, deepcopy(call)] if failure == 'duplicate' else [call]
    with pytest.raises(AssertionError):
        require_compiler_rejection(calls)


@pytest.mark.parametrize('reply', [
    {'role': 'tool', 'tool_call_id': 'call_fixture_0', 'content': 'File not found'},
    {'role': 'assistant', 'tool_call_id': 'call_fixture_0', 'content': '#!/usr/bin/env node'},
])
def test_fixture_refuses_to_count_failed_or_non_tool_reads(tmp_path, reply):
    source = tmp_path/'entry.ts'
    source.write_text('#!/usr/bin/env node\nconsole.log("fixture");\n')
    provider = Provider(source, '/workspace/candidate/src/entry.ts')
    body = {'model': 'fixture', 'stream': True, 'messages': [{'role': 'user', 'content': 'Read source'}],
            'tools': [{'function': {'name': name, 'parameters': {'properties': {'path': {}, 'content': {}}}}}
                      for name in ('read', 'write')]}
    provider.response(body)
    body['messages'].append(reply)
    with pytest.raises(AssertionError):
        provider.response(body)


def provider_request(tmp_path):
    source = tmp_path/'entry.ts'
    source.write_text('#!/usr/bin/env node\nconsole.log("fixture");\n')
    provider = Provider(source, '/workspace/candidate/src/entry.ts')
    body = {'model': 'fixture', 'stream': True, 'messages': [{'role': 'user', 'content': 'Read source'}],
            'tools': [{'function': {'name': name, 'parameters': {'properties': {'path': {}, 'content': {}}}}}
                      for name in ('read', 'write')]}
    provider.response(body)
    body['messages'].append({'role': 'tool', 'tool_call_id': 'call_fixture_0', 'content': source.read_text()})
    return provider, body


def test_fixture_finds_the_read_result_before_nested_workspace_instructions(tmp_path):
    provider, body = provider_request(tmp_path)
    body['messages'].append({'role': 'user', 'content': 'Additional instructions from: candidate/AGENTS.md'})
    response, finish = provider.response(body)
    assert finish == 'tool_calls' and response['tool_calls'][0]['function']['name'] == 'write'
    assert provider.requests[1]['read_witness_sha256']


@pytest.mark.parametrize('change', ['wrong_id', 'duplicate', 'only_in_user'])
def test_fixture_requires_one_result_bound_to_the_requested_read(tmp_path, change):
    provider, body = provider_request(tmp_path)
    if change == 'wrong_id':
        body['messages'][-1]['tool_call_id'] = 'unrelated_call'
    elif change == 'duplicate':
        body['messages'].append(deepcopy(body['messages'][-1]))
    else:
        body['messages'][-1]['role'] = 'user'
    with pytest.raises(AssertionError):
        provider.response(body)
