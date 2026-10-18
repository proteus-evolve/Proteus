"""Native accounting follows actual responses and keeps incomplete coverage unknown."""
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import threading

import pytest

from proteus.adapters.llm import LLMHarness
from proteus.adapters.minimal import MinimalHarness
from proteus.core.adapter import EpisodeResult, EpisodeSpec
from proteus.core.native_inputs import input_directory, read_native_inputs
from proteus.core.native_usage import summarize_native_inputs
from proteus.core.serialization import ContractError, content_hash


@contextmanager
def endpoint(payloads):
    replies = iter(payloads)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            reply = next(replies)
            data = reply if isinstance(reply, bytes) else json.dumps(reply).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = HTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def response(usage):
    return {'choices': [{'message': {'content': '[]'}}], 'usage': usage}


def request(root, phases=('act',)):
    return EpisodeSpec(root=root, episode=1, model='fixture', phases=phases,
                       phase_prompts={phase: 'PRIVATE-PROMPT' for phase in phases})


@pytest.mark.parametrize(('usage', 'expected'), [
    ({'prompt_tokens': 13, 'completion_tokens': 4, 'cost': 100,
      'prompt_tokens_details': {'cached_tokens': 8}, 'completion_tokens_details': {'reasoning_tokens': 2},
      'private': 'PRIVATE-PROVIDER-METADATA'}, (13, 4)),
    ({'prompt_tokens': 0, 'completion_tokens': 0}, (0, 0)),
    (None, (None, None)), ({}, (None, None)),
    ({'prompt_tokens': True, 'completion_tokens': -1}, (None, None)),
    ({'prompt_tokens': 2.5, 'completion_tokens': '7'}, (None, None)),
    ({'prompt_tokens': 9}, (9, None)),
])
def test_real_http_usage_is_normalized_without_fabricating_tokens_or_cost(tmp_path, usage, expected):
    with endpoint([response(usage)]) as url:
        adapter = LLMHarness(key='PRIVATE-API-KEY', base_url=url)
        adapter.seed(tmp_path/'harness')
        result = adapter.run_episode(request(tmp_path))
    assert result.ok
    assert (result.counters['tokens_in'], result.counters['tokens_out']) == expected
    records = read_native_inputs(tmp_path)
    usage = records[0]['outcome']['usage']
    assert usage['reported']['model_calls'] == 1
    assert usage['reported']['cost_usd'] is None and usage['reported']['wall_seconds'] >= 0
    assert 'PRIVATE-API-KEY' not in json.dumps(records)
    assert 'PRIVATE-PROVIDER-METADATA' not in json.dumps(records)
    totals = summarize_native_inputs(records)
    assert (totals['reported']['input_tokens'], totals['reported']['output_tokens']) == expected
    assert totals['reported']['cost_usd'] is None and not totals['budget_enforced']


@pytest.mark.parametrize('malformed', [False, True])
def test_failed_response_keeps_usage_when_available(tmp_path, malformed):
    body = b'not-json' if malformed else {'choices': [], 'usage': {'prompt_tokens': 6, 'completion_tokens': 2}}
    with endpoint([body]) as url:
        adapter = LLMHarness(key='fixture', base_url=url)
        adapter.seed(tmp_path/'harness')
        result = adapter.run_episode(request(tmp_path))
    assert not result.ok
    row = read_native_inputs(tmp_path)[0]
    assert row['completion'] == 'raised'
    totals = summarize_native_inputs([row])['reported']
    assert totals['input_tokens'] == (None if malformed else 6)
    assert totals['output_tokens'] == (None if malformed else 2)
    assert totals['model_calls'] == (None if malformed else 1)
    assert result.counters['tokens_in'] == totals['input_tokens']
    assert result.counters['tokens_out'] == totals['output_tokens']
    assert totals['wall_seconds'] >= 0 and totals['cost_usd'] is None


def test_partial_http_history_does_not_become_a_complete_total(tmp_path):
    with endpoint([response({'prompt_tokens': 11, 'completion_tokens': 3}), response({'completion_tokens': 5})]) as url:
        adapter = LLMHarness(key='fixture', base_url=url)
        adapter.seed(tmp_path/'harness')
        result = adapter.run_episode(request(tmp_path, ('observe', 'act')))
    assert result.ok and result.counters['tokens_in'] is None and result.counters['tokens_out'] == 8
    usage = summarize_native_inputs(read_native_inputs(tmp_path))
    assert usage['reported']['input_tokens'] is None and usage['known_subtotal']['input_tokens'] == 11
    assert usage['unknown_entries']['input_tokens'] == 1 and usage['reported']['model_calls'] == 2


def test_local_policy_zero_requires_the_builtin_mock(tmp_path):
    class NamedMock(MinimalHarness):
        name = 'named-mock'
    for name, adapter in [('mock', MinimalHarness()), ('named', NamedMock()),
                          ('custom', MinimalHarness(policy=lambda *args: []))]:
        root = tmp_path/name
        adapter.seed(root/'harness')
        assert adapter.run_episode(request(root)).ok
        usage = summarize_native_inputs(read_native_inputs(root))['reported']
        for key in ('model_calls', 'input_tokens', 'output_tokens', 'cost_usd'):
            assert usage[key] == (None if name == 'custom' else 0)
    empty = summarize_native_inputs([])
    assert empty['coverage'] == 'unavailable' and all(value is None for value in empty['reported'].values())


def rewrite(path, change):
    value = json.loads(path.read_text())
    change(value)
    value['hash'] = content_hash({key: item for key, item in value.items() if key != 'hash'})
    path.write_text(json.dumps(value))
    return value


def test_legacy_receipts_are_readable_with_unknown_usage(tmp_path):
    adapter = MinimalHarness()
    adapter.seed(tmp_path/'harness')
    assert adapter.run_episode(request(tmp_path)).ok
    directory = input_directory(tmp_path)
    start = rewrite(next(directory.glob('*.input.json')), lambda value: value.update(version=1))
    def legacy(value):
        value.update(version=1, input_hash=start['hash'])
        value.pop('usage')
    rewrite(next(directory.glob('*.outcome.json')), legacy)
    rows = read_native_inputs(tmp_path)
    assert rows[0]['completion'] == 'returned'
    assert all(value is None for value in summarize_native_inputs(rows)['reported'].values())
    assert adapter.run_episode(request(tmp_path)).ok
    mixed = summarize_native_inputs(read_native_inputs(tmp_path))
    assert mixed['boundary_count'] == 2 and mixed['reported']['input_tokens'] is None
    assert mixed['unknown_entries']['input_tokens'] == 1


@pytest.mark.parametrize('mutation', ['remove', 'normalize', 'source'])
def test_receipt_semantics_are_validated_beyond_the_hash(tmp_path, mutation):
    adapter = MinimalHarness()
    adapter.seed(tmp_path/'harness')
    assert adapter.run_episode(request(tmp_path)).ok
    def change(value):
        if mutation == 'remove':
            value.pop('usage')
        elif mutation == 'normalize':
            value['usage']['reported']['input_tokens'] = 3
        else:
            from proteus.core.native_usage import chat_usage
            value['usage'] = chat_usage({})
    rewrite(next(input_directory(tmp_path).glob('*.outcome.json')), change)
    with pytest.raises(ContractError):
        read_native_inputs(tmp_path)


def test_episode_aggregation_and_resume_preserve_unknown_counters(tmp_path):
    from proteus.core import NEUTRAL, GoalConfig
    from proteus.core.episode import RunConfig, run

    class Counting(MinimalHarness):
        def run_episode(self, spec):
            result = super().run_episode(spec)
            return EpisodeResult(episode=spec.episode, ok=result.ok, turns=result.turns,
                counters={'tokens_in': None if spec.episode == 2 else 11, 'tokens_out': 3})

    cfg = RunConfig(name='unknown', adapter=Counting(), disposition=NEUTRAL, goal=GoalConfig(),
                    root=tmp_path/'run', model='mock', episodes=3, seed=1)
    result = run(cfg)
    assert result.counters == {'tokens_in': None, 'tokens_out': 9}
    resumed = run(cfg, start=3, resume=True)
    assert resumed.counters == result.counters


def native_run(root):
    from proteus.recipes import load_recipe
    from proteus.recipes.builtins import builtin_registry
    from proteus.recipes.manifest import SourcePin
    from proteus.recipes.runner import run_recipe
    recipe = load_recipe(Path(__file__).resolve().parents[1]/'recipes/native/minimal.toml')
    registry = builtin_registry()
    lock = registry.make_lock(recipe, [SourcePin(name='proteus', repository='https://example.invalid/fixture', revision='0'*40)])
    for resume in (False, True):
        assert run_recipe(recipe, registry=registry, lock=lock, root=root, resume=resume).status.value == 'completed'


def test_native_export_is_private_read_only_and_deduplicated_across_resume(tmp_path):
    from proteus.recipes.export import export_run, report_run
    root = tmp_path/'run'
    native_run(root)
    before = {str(path.relative_to(root)): path.read_bytes() for path in root.rglob('*') if path.is_file()}
    public = report_run(root)
    owner = report_run(root, include_private=True)
    usage = public['native_accounting']
    assert usage == owner['native_accounting']
    assert usage['boundary_count'] == 8 and usage['reported']['model_calls'] == 0
    assert public['accounting']['coverage'] == 'unavailable'
    assert all('payload' not in row for row in usage['entries'])
    assert not any(path.startswith('.proteus-records/') for path in public['evidence'])
    assert sum(path.startswith('.proteus-records/') for path in owner['evidence']) == 16
    assert before == {str(path.relative_to(root)): path.read_bytes() for path in root.rglob('*') if path.is_file()}
    export_run(root, tmp_path/'export')
    assert len((tmp_path/'export/native-usage.csv').read_text().splitlines()) == 9


def test_deleted_native_receipts_cannot_silently_reduce_export_total(tmp_path):
    from proteus.recipes.export import report_run
    root = tmp_path/'run'
    native_run(root)
    directory = input_directory(root/'workspace')
    row = read_native_inputs(root/'workspace')[0]
    for suffix in ('input', 'outcome'):
        (directory/(row['input']['id']+'.'+suffix+'.json')).unlink()
    with pytest.raises(ContractError, match='snapshot differs'):
        report_run(root)


def test_native_finite_cost_caps_remain_rejected():
    from proteus.core.contracts import BudgetLimits
    from proteus.recipes import load_recipe
    from proteus.recipes.builtins import builtin_registry
    from proteus.recipes.manifest import SourcePin
    recipe = load_recipe(Path(__file__).resolve().parents[1]/'recipes/native/minimal.toml')
    recipe = replace(recipe, budget=BudgetLimits(max_cost_usd=1))
    with pytest.raises(ContractError, match='cost|budget'):
        builtin_registry().make_lock(recipe, [SourcePin(name='proteus', repository='https://example.invalid/fixture', revision='0'*40)])
