"""A real adapted HTTP harness executes changed, pinned candidate state."""

from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import re
import threading

import pytest

from proteus.adapters.llm import LLMHarness
from proteus.artifacts.codecs import FileContent, TextComponents, TextDelta
from proteus.core.contracts import OperationStatus, PluginRef
from proteus.core.serialization import ContractError
from proteus.execution.harness_candidate import HarnessEnvironmentOptions
from proteus.methods.compare import CompareOptions, Variant
from proteus.recipes.config import ActorSpec, EnvironmentSpec, ExecutionSpec, SubjectSpec
from proteus.recipes.runner import run_recipe
from test_v1_evaluation import SOURCE, setup
from proteus.examples.candidate_evaluation import plugin
from test_v1_ledger import run_ledger


@contextmanager
def state_driven_provider():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append(body)
            text = body['messages'][-1]['content']
            factor = int(re.search(r'policy.md: factor=(\d+)', text).group(1))
            task = json.loads(text.split('Task input:\n', 1)[1].split('\n\nCurrent harness state:', 1)[0])
            answer = str(factor * task)
            data = json.dumps({'choices': [{'message': {'content': json.dumps([
                {'tool': 'write_note', 'name': 'prediction', 'text': answer}])}}],
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


def configured(tmp_path, url, *, hidden=False):
    registry, store, dataset, recipe = setup(tmp_path, references=('0',), hidden=hidden)
    # setup's single task has input 0; use the multiplier itself as prediction input.
    record = replace(dataset.records[0], task=replace(dataset.records[0].task, input=2), reference='4')
    dataset = replace(dataset, records=(record,))
    (tmp_path / 'dataset.json').write_text(dataset.to_json())
    from proteus.core.serialization import content_hash
    evaluation = replace(recipe.evaluations[0], tasks=replace(recipe.evaluations[0].tasks,
        dataset_hash=content_hash(dataset)))
    base = store.put(PluginRef(name='proteus.file_tree', version='1'), {
        'STATE.md': FileContent(b'frozen base; must not be reseeded'),
        'notes/policy.md': FileContent(b'factor=999'),
    })
    artifact = store.put(PluginRef(name='proteus.text_components', version='1'),
        TextComponents(components={'notes/policy.md': 'factor=1'}))
    delta = TextDelta(base_hash=artifact.content_hash, put={'notes/policy.md': 'factor=2'})
    method = CompareOptions(evaluations=('score',), variants=(Variant(id='updated', delta=delta.to_dict()),))
    environment = HarnessEnvironmentOptions(base=base, mutable_paths=('notes/policy.md',))
    recipe = replace(recipe, method=plugin('proteus.compare', **method.to_dict()),
        actors={'target': ActorSpec(model='fixture-model', runtime=plugin('proteus.harness.llm',
            base_url=url, key_env='PROTEUS_HARNESS_CANDIDATE_KEY'))},
        subject=SubjectSpec(runner=plugin('proteus.harness_subject'), initial_artifact=artifact, actor='target'),
        execution=ExecutionSpec(environment=EnvironmentSpec(provider='host', network='unrestricted',
            backend=plugin('proteus.harness_environment', **environment.to_dict()))),
        evaluations=(evaluation,))
    return registry, store, base, recipe


def test_changed_harness_candidate_is_loaded_executed_graded_and_resumed(tmp_path, monkeypatch):
    monkeypatch.setenv('PROTEUS_HARNESS_CANDIDATE_KEY', 'fixture')
    monkeypatch.setattr(LLMHarness, 'seed', lambda *args: pytest.fail('candidate must not be reseeded'))
    with state_driven_provider() as (url, calls):
        registry, store, base, recipe = configured(tmp_path, url)
        lock = registry.make_lock(recipe, [SOURCE])
        assert {item.plugin.name for item in lock.plugins} >= {'proteus.file_tree', 'proteus.text_components'}
        result = run_recipe(recipe, registry=registry, lock=lock, root=tmp_path / 'run', artifact_source=store.root)
        assert result.status == OperationStatus.COMPLETED, result.diagnostic
        assert len(calls) == 2
        assert 'policy.md: factor=1' in calls[0]['messages'][-1]['content']
        assert 'policy.md: factor=2' in calls[1]['messages'][-1]['content']
    records = json.loads((tmp_path / 'run/execution-0001.json').read_text())
    assert [row['output'] for row in records['rollouts']] == ['2', '4']
    assert [row['rows'][0]['score'] for row in records['evaluations']] == [0, 1]
    assert records['actor_calls'] == []  # Target usage is settled exactly once as subject work.
    proofs = [row['runtime']['settings']['harness_candidate'] for row in records['rollouts']]
    assert proofs[0]['effective_harness_hash'] != proofs[1]['effective_harness_hash']
    assert all(row['base_artifact_hash'] == base.content_hash for row in proofs)
    assert all(row['usage']['model_calls'] == 1 and row['usage']['cost_usd'] is None for row in records['rollouts'])
    book = run_ledger(tmp_path / 'run')
    targets = [entry for entry in book.entries() if entry['identity']['metadata'].get('kind') == 'subject']
    assert len(targets) == 2
    assert all(entry['identity']['scopes'] == ['role:target', 'run'] for entry in targets)
    assert all(entry['identity']['metadata']['role'] == 'target' for entry in targets)
    assert run_recipe(recipe, registry=registry, lock=lock, root=tmp_path / 'run', resume=True) == result
    assert len(calls) == 2  # Provider is now stopped; completed resume cannot make another call.


def test_unsupported_harness_candidate_capability_fails_before_dispatch(tmp_path):
    registry, _, _, recipe = configured(tmp_path, 'http://127.0.0.1:1')
    recipe = replace(recipe, actors={'target': replace(recipe.actors['target'], model=None,
        runtime=plugin('proteus.harness.minimal'))})
    with pytest.raises(ContractError, match='harness.candidate_tree'):
        registry.resolve(recipe)
    assert not (tmp_path / 'run').exists()


def test_unsupported_transport_deadline_is_not_silently_ignored(tmp_path):
    registry, _, _, recipe = configured(tmp_path, 'http://127.0.0.1:1')
    recipe = replace(recipe, execution=replace(recipe.execution, timeout_seconds=1))
    with pytest.raises(ContractError, match='cannot enforce.*timeout'):
        registry.resolve(recipe)


def test_unknown_candidate_dispatch_retains_role_reservation_and_cannot_repeat(tmp_path, monkeypatch):
    from proteus.core.recovery import RecoveryRequired
    from proteus.execution.harnesses import IntegratedHarnessActor
    calls = []

    def unknown(self, *args):
        calls.append(self.integration.name)
        raise RecoveryRequired('candidate outcome is still unknown')

    monkeypatch.setattr(IntegratedHarnessActor, 'execute_candidate', unknown)
    registry, store, _, recipe = configured(tmp_path, 'http://127.0.0.1:1')
    lock = registry.make_lock(recipe, [SOURCE])
    for resume in (False, True, True):
        result = run_recipe(recipe, registry=registry, lock=lock, root=tmp_path / 'run',
            artifact_source=store.root, resume=resume)
        assert result.status == OperationStatus.FAILED and 'RecoveryRequired' in result.diagnostic
    assert calls == ['llm']
    entries = run_ledger(tmp_path / 'run').entries()
    started = [row for row in entries if row['identity']['metadata']['kind'] == 'subject']
    assert len(started) == 1 and started[0]['state'] == 'started'
    assert started[0]['identity']['scopes'] == ['role:target', 'run']


def test_harness_subject_receipt_tampering_blocks_export_and_completed_resume(tmp_path, monkeypatch):
    from proteus.core.native_inputs import input_directory
    from proteus.recipes.export import report_run
    monkeypatch.setenv('PROTEUS_HARNESS_CANDIDATE_KEY', 'fixture')
    with state_driven_provider() as (url, calls):
        registry, store, _, recipe = configured(tmp_path, url)
        lock = registry.make_lock(recipe, [SOURCE])
        result = run_recipe(recipe, registry=registry, lock=lock, root=tmp_path / 'run', artifact_source=store.root)
        assert result.status == OperationStatus.COMPLETED
        assert report_run(tmp_path / 'run')['accounting']['actual']['model_calls'] == 2
        trial = next((tmp_path / 'run').glob('evaluations/*/trials/000000'))
        path = next(input_directory(trial).glob('*.outcome.json'))
        raw = json.loads(path.read_text())
        raw['usage']['reported']['input_tokens'] = 999
        path.write_text(json.dumps(raw))
        with pytest.raises(ContractError):
            report_run(tmp_path / 'run')
        with pytest.raises(ContractError):
            run_recipe(recipe, registry=registry, lock=lock, root=tmp_path / 'run', resume=True)
        assert len(calls) == 2
