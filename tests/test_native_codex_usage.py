"""Native Codex usage is bound to the exact stream, never to tool or launch counts."""
import json
from pathlib import Path
import subprocess
import uuid

import pytest

from proteus.adapters.codex import CodexHarness
from proteus.core.adapter import EpisodeSpec
from proteus.core.native_inputs import input_directory, read_native_inputs
from proteus.core.native_usage import codex_usage, summarize_native_inputs, validate_usage
from proteus.core.serialization import ContractError


def stream(*, identity=None, tokens=13):
    return [
        {'type': 'thread.started', 'thread_id': identity or str(uuid.uuid4())},
        {'type': 'turn.started'},
        {'type': 'item.completed', 'event': 'result', 'usage': {'input_tokens': 999},
         'item': {'type': 'agent_message', 'text': 'PRIVATE-PROVIDER-CONTENT'}},
        {'type': 'turn.completed', 'usage': {'input_tokens': tokens, 'cached_input_tokens': 5,
                                           'output_tokens': 7}},
    ]


def raw(events):
    return (''.join(json.dumps(event)+'\n' for event in events)).encode()


class Sandbox:
    def __init__(self, sources, *, failure=None, fallback=False):
        self.sources = iter(sources)
        self.failure, self.fallback = failure, fallback

    def run(self, root, command, env, timeout_s, mounts=(), stop_check=None):
        source = next(self.sources)
        if not self.fallback:
            (root/'.codex-state/sessions'/Path(command[1]).name).write_bytes(source)
        if self.failure == 'timeout':
            raise subprocess.TimeoutExpired(command, timeout_s)
        return subprocess.CompletedProcess(command, 9 if self.failure == 'exit' else 0,
            source.decode() if self.fallback else '', '')


def adapter(root, monkeypatch, sandbox):
    (root/'harness/src').mkdir(parents=True)
    auth = root/'auth.json'
    auth.write_text('{}')
    publication = root/'publication'
    publication.mkdir()
    result = CodexHarness(auth_file=auth, sandbox=sandbox)
    monkeypatch.setattr(result, '_ensure_validated', lambda *args: ('', publication))
    return result


def spec(root):
    return EpisodeSpec(root=root, episode=1, model='fixture', phases=('act',),
                       phase_prompts={'act': 'LOCAL-PROMPT'}, max_turns=20)


@pytest.mark.parametrize('fallback', [False, True])
def test_actual_adapter_freezes_stream_and_keeps_each_fresh_attempt_once(tmp_path, monkeypatch, fallback):
    sources = [raw(stream(tokens=13)), raw(stream(tokens=17))]
    harness = adapter(tmp_path, monkeypatch, Sandbox(sources, fallback=fallback))
    assert harness.run_episode(spec(tmp_path)).ok
    first = read_native_inputs(tmp_path)[0]
    captured = input_directory(tmp_path)/(first['input']['id']+'.usage-source')
    assert captured.read_bytes() == sources[0]
    assert harness.run_episode(spec(tmp_path)).ok
    assert (tmp_path/'.codex-state/sessions/ep001-act.jsonl').read_bytes() == sources[1]
    assert captured.read_bytes() == sources[0]
    records = read_native_inputs(tmp_path)
    assert len(records) == 2 and all(row['outcome']['usage']['source'] == 'codex_exec_jsonl' for row in records)
    totals = summarize_native_inputs(records)
    assert totals['reported']['input_tokens'] == 30 and totals['reported']['output_tokens'] == 14
    assert totals['reported']['model_calls'] is None and totals['reported']['cost_usd'] is None
    assert 'PRIVATE-PROVIDER-CONTENT' not in json.dumps(totals)
    assert 'PRIVATE-PROVIDER-CONTENT' not in json.dumps(records)


@pytest.mark.parametrize('failure', ['timeout', 'exit'])
def test_failed_transport_does_not_claim_complete_usage_even_with_a_terminal_line(tmp_path, monkeypatch, failure):
    source = raw(stream())
    harness = adapter(tmp_path, monkeypatch, Sandbox([source], failure=failure))
    assert not harness.run_episode(spec(tmp_path)).ok
    row = read_native_inputs(tmp_path)[0]
    assert row['completion'] == ('raised' if failure == 'timeout' else 'returned')
    usage = row['outcome']['usage']
    assert usage['fields']['stream_status'] == 'completed' and not usage['fields']['transport_complete']
    assert usage['reported']['input_tokens'] is None and usage['reported']['output_tokens'] is None
    assert usage['reported']['wall_seconds'] >= 0
    assert (input_directory(tmp_path)/(row['input']['id']+'.usage-source')).read_bytes() == source


@pytest.mark.parametrize('case', ['truncated', 'invalid_json', 'negative', 'boolean', 'missing_terminal',
                                 'missing_thread', 'failed', 'duplicate_thread', 'duplicate_terminal'])
def test_invalid_or_incomplete_streams_are_unknown(case):
    events = stream()
    if case == 'negative':
        events[-1]['usage']['input_tokens'] = -1
    if case == 'boolean':
        events[-1]['usage']['input_tokens'] = True
    if case == 'missing_terminal':
        events.pop()
    if case == 'missing_thread':
        events.pop(0)
    if case == 'failed':
        events[-1] = {'type': 'turn.failed'}
    if case == 'duplicate_thread':
        events.append(events[0])
    if case == 'duplicate_terminal':
        events.append(events[-1])
    source = raw(events)
    if case == 'truncated':
        source = source[:-1]
    if case == 'invalid_json':
        source += b'not-json\n'
    usage = codex_usage(source, transport_complete=True)
    assert usage['reported']['input_tokens'] is None and usage['reported']['output_tokens'] is None
    assert usage['fields']['stream_status'] in ('invalid', 'incomplete', 'failed')


def test_complete_receipt_with_one_missing_token_field_keeps_dimension_unknown():
    events = stream()
    events[-1]['usage'].pop('input_tokens')
    usage = codex_usage(raw(events), transport_complete=True)
    assert usage['reported']['input_tokens'] is None and usage['reported']['output_tokens'] == 7
    assert codex_usage(None, transport_complete=False)['fields']['stream_status'] == 'unavailable'


@pytest.mark.parametrize('mutation', ['delete', 'change', 'symlink'])
def test_raw_stream_is_verified_during_readback(tmp_path, monkeypatch, mutation):
    harness = adapter(tmp_path, monkeypatch, Sandbox([raw(stream())]))
    assert harness.run_episode(spec(tmp_path)).ok
    path = next(input_directory(tmp_path).glob('*.usage-source'))
    if mutation == 'change':
        path.write_bytes(raw(stream(tokens=999)))
    else:
        path.unlink()
        if mutation == 'symlink':
            path.symlink_to(tmp_path/'.codex-state/sessions/ep001-act.jsonl')
    with pytest.raises(ContractError, match='source'):
        read_native_inputs(tmp_path)


def test_fresh_thread_totals_cannot_be_applied_to_resume_or_duplicate_sessions(tmp_path, monkeypatch):
    source = raw(stream())
    evidence = codex_usage(source, transport_complete=True)
    with pytest.raises(ContractError, match='fresh ephemeral'):
        validate_usage(evidence, {'scope': 'harness_cli', 'payload': ['exec', 'resume', '--json', 'prompt']})
    harness = adapter(tmp_path, monkeypatch, Sandbox([source, source]))
    assert harness.run_episode(spec(tmp_path)).ok and harness.run_episode(spec(tmp_path)).ok
    with pytest.raises(ContractError, match='reused a session'):
        summarize_native_inputs(read_native_inputs(tmp_path))


def test_recipe_export_checks_private_raw_stream_without_copying_provider_text(tmp_path, monkeypatch):
    from dataclasses import replace
    from proteus.core.contracts import OperationStatus, PluginRef
    from proteus.core.methods import MethodResult
    from proteus.recipes import load_recipe
    from proteus.recipes.builtins import builtin_registry
    from proteus.recipes.config import PluginSpec
    from proteus.recipes.export import export_run, report_run
    from proteus.recipes.runner import run_recipe
    from test_v1_plugins import plugin, SOURCE

    class Driver:
        def run(self, services, restored_state):
            root = services.root/'workspace'
            if not root.exists():
                harness = adapter(root, monkeypatch, Sandbox([raw(stream())]))
                assert harness.run_episode(spec(root)).ok
            services.native_inputs = read_native_inputs(root)
            return MethodResult(status=OperationStatus.COMPLETED)

    registry = builtin_registry()
    registry.register(plugin('fixture.native_codex', 'method', lambda _: Driver(), capabilities=('run.resume',)))
    recipe = load_recipe(Path(__file__).resolve().parents[1]/'recipes/native/minimal.toml')
    recipe = replace(recipe, actors={}, method=PluginSpec(plugin=PluginRef(name='fixture.native_codex', version='1')))
    lock = registry.make_lock(recipe, [SOURCE])
    root = tmp_path/'run'
    for resume in (False, True):
        assert run_recipe(recipe, registry=registry, lock=lock, root=root, resume=resume).status == OperationStatus.COMPLETED
    public = report_run(root)
    owner = report_run(root, include_private=True)
    assert public['native_accounting']['boundary_count'] == 1
    assert public['native_accounting']['reported']['input_tokens'] == 13
    assert not any(path.endswith('.usage-source') for path in public['evidence'])
    assert sum(path.endswith('.usage-source') for path in owner['evidence']) == 1
    assert 'PRIVATE-PROVIDER-CONTENT' not in json.dumps(owner)
    export_run(root, tmp_path/'export', include_private=True)
    assert len((tmp_path/'export/native-usage.csv').read_text().splitlines()) == 2
    next(input_directory(root/'workspace').glob('*.usage-source')).unlink()
    with pytest.raises(ContractError, match='source is missing'):
        report_run(root)
