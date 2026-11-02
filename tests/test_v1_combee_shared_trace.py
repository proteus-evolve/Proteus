"""Four production loops replay the same trace; provenance damage is rejected."""

import importlib.util
import json
from pathlib import Path
import sys

import pytest

from proteus.core.serialization import ContractError

RUNNER = Path(__file__).resolve().parents[1] / 'recipes/validation/combee-shared-trace/run.py'
spec = importlib.util.spec_from_file_location('combee_shared_trace_validation', RUNNER)
runner = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = runner
spec.loader.exec_module(runner)


@pytest.fixture(scope='module')
def replay(tmp_path_factory):
    root = tmp_path_factory.mktemp('shared-trace')
    report = runner.execute(root)
    return root, report


def test_four_methods_consume_frozen_evidence_and_dynamic_schedule(replay):
    root, report = replay
    assert runner.verify(root) == json.loads(runner.canonical_json(report))
    assert report['new_provider_calls'] == 0
    arms = {row['variant']: row for row in report['arms']}
    assert len({row['H0'] for row in arms.values()}) == 1
    assert [arm['role_calls']['reflector'] for arm in arms.values()] == [11] * 4
    assert [arm['role_calls']['curator'] for arm in arms.values()] == [11, 4, 12, 12]
    assert [arm['completed_resume_extra_actor_calls'] for arm in arms.values()] == [0] * 4
    for name, arm in arms.items():
        assert [r['task_id'] for r in arm['consumption']] == [f'sample-{i:02}' for i in range(11)]
        for group in arm['grouping_plans']:
            occurrences = [o for batch in group['occurrences'] for o in batch]
            expected_copies = {0} if name == 'naive_parallel' else {0, 1}
            for index in range(len(group['originals'])):
                assert {o['copy_index'] for o in occurrences if o['source_index'] == index} == expected_copies
    assert arms['combee_dynamic']['batch_sizes'] == [1, 2, 4, 4]
    assert arms['combee_dynamic']['batch_controller']['selected_size'] == 4
    assert arms['combee_dynamic']['controlled_clock_readings'] == [0, 1, 10, 11, 20, 21, 30, 31]


def test_changed_trace_cannot_be_read_as_original(replay, tmp_path):
    root, _ = replay
    path = tmp_path / 'trace.json'
    trace = json.loads((RUNNER.parent / 'trace.json').read_text())
    trace['samples'][0]['reflection_response'] = 'changed'
    runner.save(path, trace)
    with pytest.raises(ContractError, match='frozen trace bytes changed'):
        runner.verify(root, path)


@pytest.mark.parametrize('damage', ['record', 'receipt', 'summary'])
def test_damaged_execution_or_provenance_is_rejected(replay, damage):
    root, report = replay
    arm = report['arms'][0]
    path = (root / 'ace/run' / arm['execution_file'] if damage == 'record' else
            sorted((root / 'ace/run').rglob('trace-invocations.jsonl'))[0] if damage == 'receipt'
            else root / 'ace/audit.json')
    original = path.read_bytes()
    try:
        if damage == 'record':
            data = json.loads(original)
            data['actor_calls'][0]['result']['text'] = 'different frozen response'
            runner.save(path, data)
        elif damage == 'receipt':
            data = [json.loads(line) for line in original.splitlines()]
            data[0]['source_hash'] = 'sha256:' + '0' * 64
            path.write_text('\n'.join(json.dumps(row) for row in data) + '\n')
        else:
            data = json.loads(original)
            data['consumption'][0]['source_record_hash'] = 'sha256:' + '0' * 64
            runner.save(path, data)
        with pytest.raises(ContractError):
            runner.verify(root)
    finally:
        path.write_bytes(original)


@pytest.mark.parametrize('damage', ['source', 'recipe', 'lock'])
def test_recorded_source_config_and_lock_must_match_manifest(replay, damage):
    root, _ = replay
    path = root / ('comparison.json' if damage == 'source' else f'ace/{damage}.json')
    original = path.read_bytes()
    try:
        data = json.loads(original)
        if damage == 'source':
            data['source']['revision'] = '56d0eebfe4bcc71a1ab04d7eabf705196dd83f2b'
            data['runtime_tree'] = 'f9a0040634c388483f66882cf4a45dddc9d8636b'
        elif damage == 'recipe':
            data['budget']['max_model_calls'] += 1
        else:
            data['sources'][0]['revision'] = '56d0eebfe4bcc71a1ab04d7eabf705196dd83f2b'
        runner.save(path, data)
        with pytest.raises(ContractError):
            runner.verify(root)
    finally:
        path.write_bytes(original)
