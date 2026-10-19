"""Native finite budgets exercised at real HTTP and process-crash boundaries."""

from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from proteus.core.contracts import BudgetLimits, PluginRef
from proteus.core.native_inputs import input_directory, read_native_inputs
from proteus.core.serialization import ContractError
from proteus.recipes import load_recipe
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.config import PluginSpec
from proteus.recipes.export import report_run
from proteus.recipes.manifest import SourcePin
from proteus.recipes.runner import run_recipe


ROOT = Path(__file__).resolve().parents[1]
SOURCE = SourcePin(name='proteus', repository='https://example.invalid/fixture', revision='0'*40)


@contextmanager
def endpoint(*, malformed=False):
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            calls.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            data = b'not-json' if malformed else json.dumps({'choices': [{'message': {'content': '[]'}}],
                'usage': {'prompt_tokens': 7, 'completion_tokens': 3}}).encode()
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
        yield f'http://127.0.0.1:{server.server_port}', calls
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def config(*, url=None, run_cap=2, actor_cap=None, episodes=3, evaluators=()):
    recipe = load_recipe(ROOT/'recipes/native/minimal.toml')
    actor = recipe.actors['agent']
    if url:
        actor = replace(actor, model='fixture', runtime=PluginSpec(
            plugin=PluginRef(name='proteus.native.llm', version='1'),
            options={'base_url': url, 'key_env': 'PROTEUS_NATIVE_BUDGET_TEST_KEY'}))
    actor = replace(actor, budget=BudgetLimits(max_model_calls=actor_cap))
    return replace(recipe, actors={'agent': actor}, budget=BudgetLimits(max_model_calls=run_cap),
        method=replace(recipe.method, options={**dict(recipe.method.options), 'phases': ['act'],
            'episodes': episodes, 'evaluators': list(evaluators)}))


def execute(recipe, root, *, resume=False):
    registry = builtin_registry()
    lock = registry.make_lock(recipe, [SOURCE])
    return run_recipe(recipe, registry=registry, lock=lock, root=root, resume=resume)


@pytest.mark.parametrize(('run_cap', 'actor_cap', 'expected'), [(2, None, 2), (None, 1, 1), (2, 1, 1), (0, 3, 0)])
def test_http_cap_precedes_dispatch_and_resume_never_refills(tmp_path, monkeypatch, run_cap, actor_cap, expected):
    monkeypatch.setenv('PROTEUS_NATIVE_BUDGET_TEST_KEY', 'fixture')
    root = tmp_path/'run'
    with endpoint() as (url, calls):
        recipe = config(url=url, run_cap=run_cap, actor_cap=actor_cap)
        for resume in (False, True):
            result = execute(recipe, root, resume=resume)
            assert result.status.value == 'failed' and 'budget exhausted' in result.diagnostic
            assert len(calls) == expected
            rows = read_native_inputs(root/'workspace')
            assert len(rows) == expected  # denied attempts never acquire an input receipt
            report = report_run(root)
            assert report['native_accounting']['budget_enforced']
            assert report['native_accounting']['budget_dimensions'] == ['model_calls']
            assert len(report['native_accounting']['ledger_bindings']) == expected
            assert report['accounting']['budget_charged']['model_calls'] == expected
            assert report['accounting']['actual']['model_calls'] == expected
            assert all(row['reservation']['model_calls'] == 1 for row in
                [entry['identity'] for entry in ledger(root).entries()])


def ledger(root):
    from proteus.core.ledger import Ledger
    from proteus.recipes.manifest import RunManifest
    manifest = RunManifest.from_json((root/'manifest.json').read_text())
    return Ledger(root/'private/ledger.sqlite3', run_id=manifest.run_id,
                  config_hash=manifest.config_hash, lock_hash=manifest.lock_hash)


def test_unreported_response_retains_one_call_instead_of_zero(tmp_path, monkeypatch):
    monkeypatch.setenv('PROTEUS_NATIVE_BUDGET_TEST_KEY', 'fixture')
    with endpoint(malformed=True) as (url, calls):
        recipe = config(url=url, run_cap=1)
        assert execute(recipe, tmp_path).status.value == 'failed'
        assert execute(recipe, tmp_path, resume=True).status.value == 'failed'
        assert len(calls) == 1
    report = report_run(tmp_path)
    assert report['accounting']['actual']['model_calls'] is None
    assert report['accounting']['budget_charged']['model_calls'] == 1
    assert report['native_accounting']['reported']['model_calls'] is None


def test_pure_native_measurements_fit_zero_cap_and_completed_resume_is_free(tmp_path):
    recipe = config(run_cap=0, actor_cap=0, episodes=2,
                    evaluators=('units:notes@observe', 'tool-calls', 'step', 'contains:STATE.md:episode'))
    for resume in (False, True):
        result = execute(recipe, tmp_path, resume=resume)
        assert result.status.value == 'completed', result.diagnostic
        report = report_run(tmp_path)
        assert report['accounting']['budget_charged']['model_calls'] == 0
        assert report['native_accounting']['reported']['model_calls'] == 0
        assert len(report['native_accounting']['ledger_bindings']) == 10
    from proteus.core.native_budget import current
    assert current() is None


@pytest.mark.parametrize('backend', ['pi', 'dsh', 'codex'])
def test_opaque_native_backends_reject_finite_caps_before_publication(tmp_path, backend):
    recipe = config()
    actor = replace(recipe.actors['agent'], model='fixture', session='handoff',
        runtime=PluginSpec(plugin=PluginRef(name='proteus.native.'+backend, version='1')))
    recipe = replace(recipe, actors={'agent': actor})
    with pytest.raises(ContractError, match='finite native'):
        execute(recipe, tmp_path/'run')
    assert not (tmp_path/'run/manifest.json').exists()


@pytest.mark.parametrize('unsupported', ['local:fixture', 'mbpp:1'])
def test_benchmark_evaluators_with_no_bound_are_rejected_before_loading(tmp_path, unsupported):
    with pytest.raises(ContractError, match='model-free evaluators'):
        execute(config(evaluators=(unsupported,)), tmp_path/'run')
    assert not (tmp_path/'run/manifest.json').exists()


CRASH = '''
import json, os, sys
from pathlib import Path
from test_native_budget import config, execute
from proteus.core.native_budget import NativeBudget
from proteus.core.ledger import Ledger
from proteus.core import native_inputs
mode, root, url = sys.argv[1:]
if mode == 'reserved':
    original = Ledger.reserve
    def reserve(self, *args, **kwargs):
        original(self, *args, **kwargs)
        os._exit(71)
    Ledger.reserve = reserve
elif mode == 'started':
    original = NativeBudget.prepare
    def prepare(self, *args, **kwargs):
        original(self, *args, **kwargs)
        os._exit(71)
    NativeBudget.prepare = prepare
elif mode == 'input':
    original = native_inputs._publish
    def publish(path, value):
        result = original(path, value)
        if str(path).endswith('.input.json'):
            os._exit(71)
        return result
    native_inputs._publish = publish
elif mode == 'outcome':
    NativeBudget.finish = lambda *args: os._exit(71)
execute(config(url=url, run_cap=2, episodes=1), Path(root))
'''


@pytest.mark.parametrize('mode', ['reserved', 'started', 'input', 'outcome'])
def test_process_crash_recovery_uses_durable_order_not_transport_guessing(tmp_path, monkeypatch, mode):
    monkeypatch.setenv('PROTEUS_NATIVE_BUDGET_TEST_KEY', 'fixture')
    env = {**os.environ, 'PYTHONPATH': os.pathsep.join((str(ROOT), str(ROOT/'tests')))}
    with endpoint() as (url, calls):
        process = subprocess.run([sys.executable, '-c', CRASH, mode, str(tmp_path), url],
                                 env=env, capture_output=True, text=True, timeout=25)
        assert process.returncode == 71, process.stderr
        assert len(calls) == (1 if mode == 'outcome' else 0)
        recipe = config(url=url, run_cap=2, episodes=1)
        for _ in range(2):
            result = execute(recipe, tmp_path, resume=True)
            if mode in ('started', 'input'):
                assert result.status.value == 'failed' and 'outcome is unknown' in result.diagnostic
                assert len(calls) == 0 and ledger(tmp_path).balance()['charged']['model_calls'] == 1
                assert ledger(tmp_path).balance()['active_slots'] == 1
                with pytest.raises(ContractError, match='settled operations'):
                    report_run(tmp_path)
            else:
                assert result.status.value == 'completed', result.diagnostic
                expected = 2 if mode == 'outcome' else 1
                assert len(calls) == expected
                report = report_run(tmp_path)
                assert report['accounting']['budget_charged']['model_calls'] == expected
                assert len(report['native_accounting']['ledger_bindings']) == expected


def test_export_detects_rehashed_usage_or_missing_ledger(tmp_path):
    from test_native_usage import rewrite
    assert execute(config(run_cap=0, episodes=1), tmp_path).status.value == 'completed'
    path = next(input_directory(tmp_path/'workspace').glob('*.outcome.json'))
    before = path.read_bytes()
    def change(value):
        value['usage']['reported']['wall_seconds'] += 1
    rewrite(path, change)
    # Repair the execution copy as well; the independently stored settlement still binds the outcome.
    snapshot = tmp_path/'execution-0001.json'
    data = json.loads(snapshot.read_text())
    data['native_inputs'] = read_native_inputs(tmp_path/'workspace')
    snapshot.write_text(json.dumps(data))
    with pytest.raises(ContractError, match='settlement differs'):
        report_run(tmp_path)
    path.write_bytes(before)
    data['native_inputs'] = read_native_inputs(tmp_path/'workspace')
    snapshot.write_text(json.dumps(data))
    (tmp_path/'private/ledger.sqlite3').unlink()
    with pytest.raises(ContractError, match='ledger is missing'):
        report_run(tmp_path)


@pytest.mark.parametrize('mutation', ['limit', 'membership'])
def test_saved_limit_or_membership_cannot_silently_change(tmp_path, mutation):
    import sqlite3
    recipe = config(run_cap=0, episodes=1)
    assert execute(recipe, tmp_path).status.value == 'completed'
    with sqlite3.connect(tmp_path/'private/ledger.sqlite3') as db:
        if mutation == 'limit':
            db.execute('UPDATE scopes SET limits_json=? WHERE id=?',
                       (json.dumps(BudgetLimits(max_model_calls=10).to_dict()), 'run'))
        else:
            db.execute('DELETE FROM membership WHERE scope=?', ('role:agent',))
    with pytest.raises(ContractError, match='scope'):
        report_run(tmp_path)
    result = execute(recipe, tmp_path, resume=True)
    assert result.status.value == 'failed' and 'scope' in result.diagnostic


def test_http_multiple_phases_and_initial_evaluation_share_one_budget(tmp_path, monkeypatch):
    monkeypatch.setenv('PROTEUS_NATIVE_BUDGET_TEST_KEY', 'fixture')
    with endpoint() as (url, calls):
        recipe = config(url=url, run_cap=2, episodes=1, evaluators=('tool-calls',))
        recipe = replace(recipe, method=replace(recipe.method, options={**dict(recipe.method.options),
            'phases': ['observe', 'act', 'reflect'], 'eval_initial': ['tool-calls']}))
        result = execute(recipe, tmp_path)
        assert result.status.value == 'failed' and len(calls) == 2
        assert ledger(tmp_path).balance()['charged']['model_calls'] == 2
        report = report_run(tmp_path)
        assert report['native_accounting']['reported']['model_calls'] == 2
        assert any(row['scope'] == 'native_evaluator' and row['episode'] == 0
                   for row in report['native_accounting']['entries'])


def test_custom_adapter_cannot_inherit_a_zero_call_claim(tmp_path):
    from proteus.adapters.minimal import MinimalHarness
    from proteus.core.native_budget import budget_scope, current
    from types import SimpleNamespace
    services = SimpleNamespace(recipe=config(run_cap=0), root=tmp_path)
    with pytest.raises(ContractError, match='unmodified bundled adapter'):
        with budget_scope(services, MinimalHarness(policy=lambda *args: []), ()):
            pytest.fail('custom adapter admitted')
    assert current() is None
