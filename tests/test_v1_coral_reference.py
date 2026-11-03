"""Compare policy traces and packaged reference bytes with the pinned upstream checkout."""

import ast
import importlib.util
import os
from pathlib import Path
import random
import subprocess
import sys

import pytest

from proteus.events.policies import CadenceRule
from proteus.methods.coral.policy import CoralOptions, CoralPolicy
from test_v1_events import observation


REVISION = '0123dfb939b35228cf2c1fde224cd0561e727408'


@pytest.fixture
def source():
    location = os.environ.get('PROTEUS_CORAL_SOURCE')
    if not location:
        pytest.skip('set PROTEUS_CORAL_SOURCE to the pinned CORAL checkout')
    root = Path(location)
    assert subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip() == REVISION
    return root


def test_reference_bytes_license_prompts_and_manager_lifecycle(source):
    from proteus.methods import coral
    package = Path(coral.__file__).parent
    assert (package/'_reference_heartbeat.py').read_bytes() == (source/'coral/agent/heartbeat.py').read_bytes()
    assert (package/'LICENSE-CORAL.txt').read_bytes() == (source/'LICENSE').read_bytes()
    for name in ('reflect', 'consolidate', 'pivot'):
        assert (package/f'{name}.reference.md').read_bytes() == (source/f'coral/hub/prompts/{name}.md').read_bytes()
    tree = ast.parse((source/'coral/agent/manager.py').read_text())
    function, = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
                 and node.name == '_get_heartbeat_runner']
    assert any(isinstance(node, ast.Return) and isinstance(node.value, ast.Call)
               and isinstance(node.value.func, ast.Name) and node.value.func.id == 'HeartbeatRunner'
               for node in ast.walk(function))


@pytest.mark.parametrize('profile', ['reference_manager', 'persistent_cooldown'])
@pytest.mark.parametrize('minimize', [True, False])
def test_every_emitted_action_matches_actual_reference_runner(source, profile, minimize):
    spec = importlib.util.spec_from_file_location('_coral_upstream_heartbeat', source/'coral/agent/heartbeat.py')
    upstream = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = upstream
    spec.loader.exec_module(upstream)
    rules = (CadenceRule(name='reflect', trigger='evaluations', every=2, prompt='reflect'),
             CadenceRule(name='consolidate', trigger='evaluations', scope='global', every=5, prompt='consolidate'),
             CadenceRule(name='pivot', trigger='plateau', every=3, epsilon=0.1, prompt='pivot'))
    policy = CoralPolicy(CoralOptions(rules=rules, profile=profile, minimize=minimize))
    actions = [upstream.HeartbeatAction(name='reflect', every=2, prompt='reflect'),
               upstream.HeartbeatAction(name='consolidate', every=5, prompt='consolidate', is_global=True),
               upstream.HeartbeatAction(name='pivot', every=3, prompt='pivot', trigger='plateau', options={'epsilon': 0.1})]
    state = policy.initial_state()
    runners, counts, histories = {}, {'a': 0, 'b': 0}, {'a': [], 'b': []}
    rng = random.Random(42)
    for index in range(120):
        worker, score, sample = rng.choice(('a', 'b')), rng.choice((None, 0.1, 0.2, 0.3, 0.9)), rng.random() > 0.1
        counts[worker] += 1
        if sample:
            histories[worker].append(score)
        if profile == 'reference_manager' or worker not in runners:
            runners[worker] = upstream.HeartbeatRunner(actions)
        expected = runners[worker].check(local_eval_count=counts[worker], global_eval_count=index + 1,
                                         score_history=histories[worker], minimize=minimize)
        transition = policy.advance(observation(index, worker=worker, score=score, sample=sample), state)
        assert [(item.name, item.prompt) for item in transition.actions] == [(item.name, item.prompt) for item in expected]
        # Serialize on every transition to exercise persisted, not object-local, cooldowns.
        state = type(state).from_json(transition.state.to_json())
