"""Infrastructure failures cannot satisfy the real-source compiler gate."""
from copy import deepcopy

import pytest

from check_native_source_docker import require_compiler_rejection


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
