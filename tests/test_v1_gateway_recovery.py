"""Recover the controller while independent workers retain their original tickets."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import subprocess
import sys
import threading

import pytest

from proteus.core.methods import MethodResult
from proteus.core.serialization import ContractError
from proteus.gateway.records import GatewayResult
from proteus.recipes.runner import run_recipe
from test_v1_gateway import setup, services, submission
from test_v1_ledger import run_ledger


@pytest.mark.parametrize('reader', ['fixture', 'fixture_post', 'client'])
def test_worker_retries_a_truncated_response_after_controller_exit(monkeypatch, reader):
    from proteus.examples import _gateway_worker_fixture
    from proteus.gateway.client import GatewayClient
    calls = []
    posts = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            calls.append(self.path)
            body = b'{"status":"completed"}'
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body[:5] if len(calls) == 1 else body)
            self.wfile.flush()
            self.close_connection = True

        def do_POST(self):
            posts.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            self.do_GET()

    server = HTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01})
    thread.start()
    url = f'http://127.0.0.1:{server.server_port}'
    monkeypatch.setenv('PROTEUS_EVALUATION_URL', url)
    monkeypatch.setenv('PROTEUS_EVALUATION_TOKEN', 'offline-test')
    try:
        path = '/v1/submissions' if reader == 'fixture_post' else '/v1/submissions/ticket'
        body = {'operation_id': 'same-operation', 'files': []} if reader == 'fixture_post' else None
        value = (_gateway_worker_fixture.request(path, body) if reader.startswith('fixture')
                 else GatewayClient(url, 'offline-test').wait('ticket', timeout=5))
        assert value == {'status': 'completed'} and calls == [path] * 2
        assert posts == ([body, body] if body is not None else [])
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


WORKER = r'''
import os, sys
from pathlib import Path
from proteus.core.recovery import Journal
from proteus.examples.evaluation_gateway import register
from proteus.gateway.store import GatewayStore
from proteus.recipes import load_recipe
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.manifest import DependencyLock
from proteus.recipes.runner import run_recipe
root, boundary = Path(sys.argv[1]), sys.argv[2]
registry = builtin_registry(); register(registry)
recipe = load_recipe(root/'recipe.json')
lock = DependencyLock.from_json((root/'lock.json').read_text())
if boundary.startswith('store.'):
    method = boundary.split('.')[1]
    original = getattr(GatewayStore, method)
    def interrupted(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        os._exit(86)
    setattr(GatewayStore, method, interrupted)
else:
    original = Journal.call
    def interrupted(self, kind, identity, *args, **kwargs):
        result = original(self, kind, identity, *args, **kwargs)
        if kind == boundary and (kind.startswith('gateway.') or
                str(identity.get('operation_id', '')).startswith('gateway:')):
            os._exit(86)
        return result
    Journal.call = interrupted
run_recipe(recipe, registry=registry, lock=lock, root=root/'run', artifact_source=root/'study/artifact-store')
'''


@pytest.mark.parametrize('boundary', [
    'store.admit', 'store.claim', 'gateway.claim', 'evaluation.finish',
    'store.finish', 'gateway.finish',
])
def test_controller_hard_exit_keeps_workers_tickets_evaluations_and_shared_attempts(tmp_path, boundary):
    recipe, registry, lock = setup(tmp_path)
    stopped = subprocess.run([sys.executable, '-c', WORKER, str(tmp_path), boundary],
                             text=True, capture_output=True, timeout=35)
    assert stopped.returncode == 86, stopped.stdout + stopped.stderr
    result = run_recipe(recipe, registry=registry, lock=lock, root=tmp_path/'run', resume=True)
    diagnostics = '\n'.join(path.read_text()[-2000:] for path in (tmp_path/'run/private/workers').rglob('stderr.log'))
    assert result.status.value == 'completed', (result.diagnostic, diagnostics)
    current = services(tmp_path, recipe, registry, lock)
    records = json.loads((tmp_path/'run/execution-0001.json').read_text())
    assert len(records['rollouts']) == 5
    assert sorted([row['score'] for row in batch['rows']] for batch in records['evaluations']) == [[0, 0], [1, 1]]
    assert len(current.knowledge('research').store.snapshot().revisions) == 2
    assert not current.gateway('formal').store.pending()
    for name in ('a', 'b'):
        worker = current.worker(name).controller
        # A controller exit must not create a replacement worker generation.
        assert len(list(worker.private.glob('*/request.json'))) == 1
        assert worker.poll(worker.head()).state == 'completed'
        receipt = json.loads((worker.workspace/'ticket.json').read_text())
        response = json.loads((worker.workspace/'response.json').read_text())
        assert current.gateway('formal').response(name, receipt['ticket_id']) == response
    book = run_ledger(tmp_path/'run')
    assert book.balance()['charged']['evaluations'] == 5
    assert book.balance()['active_slots'] == 0
    counts = {}
    for entry in book.entries():
        metadata = entry['identity']['metadata']
        if metadata['kind'] == 'evaluation_cohort':
            counts[metadata['worker_id']] = counts.get(metadata['worker_id'], 0) + 1
    assert counts == {None: 1, 'a': 1, 'b': 1}
    before = book.events()
    assert run_recipe(recipe, registry=registry, lock=lock, root=tmp_path/'run', resume=True) == result
    assert book.events() == before


@pytest.mark.parametrize('pending_limit,evaluation_limit,error', [
    (1, 4, 'queue is full'), (2, 2, 'admission budget exhausted'),
])
def test_distinct_concurrent_submissions_cannot_overbook_queue_or_admission_budget(
        tmp_path, pending_limit, evaluation_limit, error):
    recipe, registry, lock = setup(tmp_path)
    spec = replace(recipe.gateways[0], max_pending_per_worker=pending_limit,
                   max_evaluations_per_worker=evaluation_limit)
    recipe = replace(recipe, gateways=(spec,))
    barrier = threading.Barrier(2)

    def submit(number):
        gateway = services(tmp_path, recipe, registry, lock).gateway('formal')
        # Force two separately opened stores to race after their initial capacity check.
        gateway.store.preflight('a', submission(operation=str(number)))
        barrier.wait(timeout=5)
        try:
            return gateway.submit('a', submission(operation=str(number)))
        except ContractError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(submit, range(2)))
    assert sum(isinstance(item, str) and error in item for item in outcomes) == 1
    store = services(tmp_path, recipe, registry, lock).gateway('formal').store
    assert len(store.pending()) == 1
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda name: store.claim(name).ticket, ('one', 'two')))
    assert sum(ticket is not None for ticket in claims) == 1


@pytest.mark.parametrize('raises', [False, True])
def test_pending_gateway_requests_prevent_terminal_method_result(tmp_path, monkeypatch, raises):
    from proteus.examples.evaluation_gateway import GatewayMethod
    recipe, registry, lock = setup(tmp_path)

    def early(self, current, state):
        current.gateway('formal').submit('a', submission())
        if raises:
            raise ValueError('method failed before dispatching queued evaluation')
        return MethodResult()

    monkeypatch.setattr(GatewayMethod, 'run', early)
    result = run_recipe(recipe, registry=registry, lock=lock, root=tmp_path/'run',
                        artifact_source=tmp_path/'study/artifact-store')
    assert result.status.value == 'failed' and 'pending gateway' in result.diagnostic
    requests = [json.loads(path.read_text())['value'] for path in (tmp_path/'run/journal/operations').glob('*.request.json')]
    assert all(record['kind'] != 'method.result' for record in requests)


def test_failed_evaluation_hides_private_diagnostics_and_keeps_admission_charge(tmp_path, monkeypatch):
    recipe, registry, lock = setup(tmp_path)
    current = services(tmp_path, recipe, registry, lock)
    gateway = current.gateway('formal')
    ticket = gateway.submit('a', submission())
    assert gateway.claim('first') == ticket

    def private_failure(*args, **kwargs):
        raise ValueError('PRIVATE GRADER SECRET')

    monkeypatch.setattr(current, 'evaluate', private_failure)
    result = gateway.process(ticket)
    assert result.status == 'failed' and result.evidence is None
    assert 'PRIVATE GRADER SECRET' not in json.dumps(gateway.response('a', ticket.id()))
    with pytest.raises(ContractError, match='admission budget exhausted'):
        gateway.submit('a', submission(operation='retry-as-new'))
    with pytest.raises(ContractError, match='changed after publication'):
        gateway.store.finish(GatewayResult(ticket=ticket, status='failed', diagnostic='changed'))
