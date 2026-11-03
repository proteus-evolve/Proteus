"""Pi usage follows persisted categories and never treats SDK default zeros as known."""
import json
from pathlib import Path
import subprocess

import pytest

from check_native_pi_package import check, entries, raw
from proteus.adapters.pi import PiHarness
from proteus.core.adapter import EpisodeSpec
from proteus.core.native_inputs import input_directory, read_native_inputs
from proteus.core.native_pi_usage import capture_pi_sources, fingerprint
from proteus.core.native_usage import pi_usage, summarize_native_inputs, validate_usage
from proteus.core.serialization import ContractError


def evidence(root, values, *, complete=True, previous=None):
    path = root/'fixture.jsonl'
    path.write_bytes(raw(values))
    return pi_usage(capture_pi_sources([path], previous or {}), transport_complete=complete)


def test_actual_adapter_preserves_independent_sessions_and_all_usage_categories():
    check()


@pytest.mark.parametrize('field', ['input', 'cacheRead', 'cacheWrite', 'output'])
@pytest.mark.parametrize('value', [None, -1, True, 1.5, '4'])
def test_missing_or_invalid_components_remain_unknown(tmp_path, field, value):
    values = entries()
    values[1]['message']['usage'][field] = value
    usage = evidence(tmp_path, values)
    dimension = 'output_tokens' if field == 'output' else 'input_tokens'
    other = 'input_tokens' if dimension == 'output_tokens' else 'output_tokens'
    assert usage['reported'][dimension] is None and usage['reported'][other] > 0
    assert usage['fields']['unknown_samples'][dimension] == 1


@pytest.mark.parametrize('kind', ['assistant', 'compaction', 'branch_summary', 'toolResult'])
def test_ambiguous_zero_defaults_cannot_claim_zero_usage(tmp_path, kind):
    values = entries()
    for entry in values[1:]:
        target = entry['message'] if entry['type'] == 'message' else entry
        if target.get('role', entry['type']) == kind:
            target['usage'] = {'input':0, 'output':0, 'cacheRead':0, 'cacheWrite':0}
    usage = evidence(tmp_path, values)
    assert usage['reported']['input_tokens'] is None and usage['reported']['output_tokens'] is None


@pytest.mark.parametrize('kind', ['assistant', 'compaction', 'branch_summary'])
def test_missing_required_usage_does_not_shrink_the_total(tmp_path, kind):
    values = entries()
    for entry in values[1:]:
        target = entry['message'] if entry['type'] == 'message' else entry
        if target.get('role', entry['type']) == kind:
            target.pop('usage')
    usage = evidence(tmp_path, values)
    assert usage['reported']['input_tokens'] is None and usage['reported']['output_tokens'] is None


@pytest.mark.parametrize('stop', ['error', 'aborted', 'pending', 'deferred', 'toolUse'])
def test_unfinished_final_assistant_is_not_complete_accounting(tmp_path, stop):
    values = entries()
    values[-1]['message']['stopReason'] = stop
    usage = evidence(tmp_path, values)
    assert usage['fields']['stream_status'] == 'incomplete'
    assert usage['reported']['input_tokens'] is None


@pytest.mark.parametrize('mutation', ['old_version', 'duplicate_id', 'missing_parent', 'inherited', 'new_type'])
def test_unsupported_or_inherited_histories_cannot_double_count(tmp_path, mutation):
    values = entries()
    if mutation == 'old_version':
        values[0]['version'] = 2
    elif mutation == 'duplicate_id':
        values.append(values[-1])
    elif mutation == 'missing_parent':
        values[1]['parentId'] = 'missing'
    elif mutation == 'inherited':
        values[0]['parentSession'] = '/state/prior.jsonl'
    else:
        values[-1]['type'] = 'future_usage'
    usage = evidence(tmp_path, values)
    assert usage['fields']['stream_status'] in ('invalid', 'unsupported_history')
    assert usage['reported']['input_tokens'] is None


def test_missing_tool_usage_is_optional_but_missing_summary_usage_is_unknown(tmp_path):
    values = entries()
    values[2]['message'].pop('usage')
    usage = evidence(tmp_path, values)
    assert usage['reported']['input_tokens'] == 27 and usage['reported']['output_tokens'] == 12
    assert usage['fields']['samples']['toolResult'] == 0


def test_changes_to_previous_sessions_invalidate_new_log_totals(tmp_path):
    old = tmp_path/'old.jsonl'
    old.write_bytes(raw(entries()))
    before = {old: fingerprint(old)}
    old.write_bytes(old.read_bytes()+b'\n')
    usage = evidence(tmp_path, entries(), previous=before)
    assert usage['fields']['stream_status'] == 'incomplete' and usage['reported']['input_tokens'] is None


def test_snapshot_refuses_symlinks_and_truncated_json_without_reading_targets(tmp_path):
    target = tmp_path/'private'
    target.write_text('PRIVATE-FILE')
    log = tmp_path/'bad.jsonl'
    log.symlink_to(target)
    source = capture_pi_sources([log], {})
    assert 'PRIVATE-FILE' not in source.decode()
    assert pi_usage(source, transport_complete=True)['reported']['input_tokens'] is None
    log.unlink()
    log.write_bytes(raw(entries())[:-1])
    assert pi_usage(capture_pi_sources([log], {}), transport_complete=True)['fields']['stream_status'] == 'invalid'


@pytest.mark.parametrize('failure', ['timeout', 'exit'])
def test_failed_transport_keeps_raw_logs_and_unknown_totals(tmp_path, failure):
    (tmp_path/'harness').mkdir()
    class Sandbox:
        def run(self, root, command, env, timeout_s, mounts=(), stop_check=None):
            (root/'.pi-state/session.jsonl').write_bytes(raw(entries()))
            if failure == 'timeout':
                raise subprocess.TimeoutExpired(command, timeout_s)
            return subprocess.CompletedProcess(command, 1, '', '')
    adapter = PiHarness(key='fixture', sandbox=Sandbox())
    result = adapter.run_episode(EpisodeSpec(root=tmp_path, episode=1, model='fixture',
        phases=('act',), phase_prompts={'act':'fixture'}))
    assert not result.ok
    records = read_native_inputs(tmp_path)
    assert records[0]['completion'] == ('raised' if failure == 'timeout' else 'returned')
    assert summarize_native_inputs(records)['reported']['input_tokens'] is None
    source = next(input_directory(tmp_path).glob('*.usage-source'))
    source.write_bytes(b'{}')
    with pytest.raises(ContractError, match='source differs'):
        read_native_inputs(tmp_path)


def test_usage_source_requires_a_fresh_persisted_pi_invocation(tmp_path):
    usage = evidence(tmp_path, entries())
    with pytest.raises(ContractError, match='new persisted sessions'):
        validate_usage(usage, {'scope':'harness_cli', 'payload':['--session-dir','/state','-p','--continue','prompt']})


def test_recipe_export_retains_pi_source_privately_without_repeating_attempts(tmp_path, monkeypatch):
    from dataclasses import replace
    from proteus.core.contracts import OperationStatus, PluginRef
    from proteus.core.methods import MethodResult
    from proteus.recipes import load_recipe
    from proteus.recipes.builtins import builtin_registry
    from proteus.recipes.config import PluginSpec
    from proteus.recipes.export import report_run
    from proteus.recipes.runner import run_recipe
    from test_v1_plugins import plugin, SOURCE
    class Sandbox:
        def run(self, root, command, env, timeout_s, mounts=(), stop_check=None):
            (root/'.pi-state/fresh.jsonl').write_bytes(raw(entries()))
            return subprocess.CompletedProcess(command, 0, '', '')
    class Driver:
        def run(self, services, restored_state):
            root = services.root/'workspace'
            if not root.exists():
                (root/'harness').mkdir(parents=True)
                assert PiHarness(key='fixture', sandbox=Sandbox()).run_episode(EpisodeSpec(
                    root=root, episode=1, model='fixture', phases=('act',), phase_prompts={'act':'fixture'})).ok
            services.native_inputs = read_native_inputs(root)
            return MethodResult(status=OperationStatus.COMPLETED)
    registry = builtin_registry()
    registry.register(plugin('fixture.native_pi', 'method', lambda _: Driver(), capabilities=('run.resume',)))
    recipe = load_recipe(Path(__file__).resolve().parents[1]/'recipes/native/minimal.toml')
    recipe = replace(recipe, actors={}, method=PluginSpec(plugin=PluginRef(name='fixture.native_pi', version='1')))
    lock = registry.make_lock(recipe, [SOURCE])
    root = tmp_path/'run'
    for resume in (False, True):
        assert run_recipe(recipe, registry=registry, lock=lock, root=root, resume=resume).status == OperationStatus.COMPLETED
    public, owner = report_run(root), report_run(root, include_private=True)
    assert public['native_accounting']['boundary_count'] == 1
    assert public['native_accounting']['reported']['input_tokens'] == 30
    assert sum(path.endswith('.usage-source') for path in owner['evidence']) == 1
    assert not any(path.endswith('.usage-source') for path in public['evidence'])
    assert 'PRIVATE-SUMMARY' not in json.dumps(owner)
