"""Authenticated admission, queue receipts and exact released feedback."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import urllib.error

import pytest

from proteus.artifacts.codecs import FileWrite
from proteus.core.methods import RunServices
from proteus.core.serialization import ContractError, content_hash
from proteus.examples.evaluation_gateway import prepare, register
from proteus.gateway.client import GatewayClient
from proteus.gateway.records import GatewayResult, GatewaySubmission
from proteus.recipes import load_recipe
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.runner import run_recipe
from test_v1_evaluation import SOURCE
from test_v1_ledger import run_ledger


def setup(root):
    recipe = load_recipe(prepare(root / 'study'))
    registry = builtin_registry()
    register(registry)
    recipe = registry.resolve(recipe)
    lock = registry.make_lock(recipe, [SOURCE])
    (root / 'recipe.json').write_text(recipe.to_json())
    (root / 'lock.json').write_text(lock.to_json())
    return recipe, registry, lock


def services(root, recipe, registry, lock):
    return RunServices(run_id='run', recipe=recipe, registry=registry, root=root / 'run', lock_hash=content_hash(lock))


def client(gateway, worker):
    environment = gateway.connection(worker)
    return GatewayClient(environment['PROTEUS_EVALUATION_URL'], environment['PROTEUS_EVALUATION_TOKEN'])


def submission(operation='first', evaluation='validation', data=b'print("candidate")'):
    return GatewaySubmission(operation_id=operation, evaluation_id=evaluation,
                             files=(FileWrite.from_bytes('main.py', data),))


def test_http_identity_scope_idempotency_and_atomic_queue_capacity(tmp_path):
    recipe, registry, lock = setup(tmp_path)
    gateway = services(tmp_path, recipe, registry, lock).gateway('formal')
    with gateway.serve():
        a, b = client(gateway, 'a'), client(gateway, 'b')
        with pytest.raises(urllib.error.HTTPError) as error:
            GatewayClient(a.url, 'wrong-credential').submit(submission())
        assert error.value.code == 401
        with pytest.raises(urllib.error.HTTPError):
            a.submit(submission(evaluation='test'))
        with pytest.raises(urllib.error.HTTPError):
            a._request('/v1/submissions', {**submission().to_dict(), 'score': 1, 'worker_id': 'b'})
        with ThreadPoolExecutor(max_workers=2) as pool:
            accepted = list(pool.map(lambda _: a.submit(submission()), range(2)))
        assert accepted[0] == accepted[1]
        ticket = accepted[0]['ticket_id']
        with pytest.raises(urllib.error.HTTPError):
            b.poll(ticket)
        with pytest.raises(urllib.error.HTTPError):
            a.submit(submission(data=b'changed'))
        with pytest.raises(urllib.error.HTTPError):
            a.submit(submission(operation='other'))
        assert a.poll(ticket)['status'] == 'queued'
        assert len(gateway.store.pending()) == 1


def test_empty_and_claimed_reads_keep_their_original_outcome_and_quota(tmp_path):
    recipe, registry, lock = setup(tmp_path)
    gateway = services(tmp_path, recipe, registry, lock).gateway('formal')
    assert gateway.store.claim('empty').ticket is None
    ticket = gateway.submit('a', submission())
    assert gateway.store.claim('empty').ticket is None
    assert gateway.store.claim('first').ticket == ticket
    assert gateway.store.claim('different').ticket is None
    failure = GatewayResult(ticket=ticket, status='failed', diagnostic='test failure')
    assert gateway.store.finish(failure) == failure
    assert gateway.store.finish(failure) == failure
    assert gateway.store.claim('first').ticket == ticket
    assert gateway.submit('a', submission()) == ticket
    with pytest.raises(ContractError, match='admission budget exhausted'):
        gateway.submit('a', submission(operation='second'))


def test_gateway_restarts_on_same_address_with_same_credential_and_queue(tmp_path):
    recipe, registry, lock = setup(tmp_path)
    current = services(tmp_path, recipe, registry, lock)
    gateway = current.gateway('formal')
    with gateway.serve():
        a = client(gateway, 'a')
        ticket = a.submit(submission())
    recreated = current.gateway('formal')
    with recreated.serve():
        assert client(recreated, 'a').url == a.url
        assert client(recreated, 'a').token == a.token
        assert a.poll(ticket['ticket_id'])['status'] == 'queued'
    (gateway.root / 'endpoint.json').unlink()
    with pytest.raises(ContractError, match='endpoint is missing'):
        with recreated.serve():
            pass


def test_preflight_rejects_private_evaluations_and_cross_role_evidence(tmp_path):
    recipe, _, _ = setup(tmp_path)
    spec = recipe.gateways[0]
    with pytest.raises(ContractError, match='private evaluations'):
        replace(recipe, gateways=(replace(spec, evaluations=('test',)),))
    with pytest.raises(ContractError, match='another role'):
        replace(recipe, gateways=(replace(spec, members={'a': 'missing'}),), workers=(recipe.workers[0],))


def test_missing_durable_queue_cannot_reset_credentials_or_admission_budget(tmp_path):
    recipe, registry, lock = setup(tmp_path)
    gateway = services(tmp_path, recipe, registry, lock).gateway('formal')
    gateway.submit('a', submission())
    gateway.store.path.unlink()
    with pytest.raises(ContractError, match='queue is missing'):
        services(tmp_path, recipe, registry, lock).gateway('formal').store


def test_gateway_endpoint_has_one_owner_and_rejects_port_reassignment(tmp_path):
    from proteus.core.recovery import RecoveryRequired
    import socket
    recipe, registry, lock = setup(tmp_path)
    current = services(tmp_path, recipe, registry, lock)
    gateway = current.gateway('formal')
    with gateway.serve():
        endpoint = json.loads((gateway.root/'endpoint.json').read_text())
        with pytest.raises(RecoveryRequired, match='another controller'):
            with current.gateway('formal').serve():
                pass
    with socket.socket() as occupied:
        occupied.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        occupied.bind(('127.0.0.1', endpoint['port']))
        occupied.listen()
        with pytest.raises(OSError):
            with current.gateway('formal').serve():
                pass
        assert json.loads((gateway.root/'endpoint.json').read_text()) == endpoint


def test_real_workers_submit_frozen_candidates_and_receive_only_released_feedback(tmp_path):
    recipe, registry, lock = setup(tmp_path)
    result = run_recipe(recipe, registry=registry, lock=lock, root=tmp_path/'run', artifact_source=tmp_path/'study/artifact-store')
    assert result.status.value == 'completed', result.diagnostic
    current = services(tmp_path, recipe, registry, lock)
    responses = []
    for name in ('a', 'b'):
        worker = current.worker(name).controller
        response = json.loads((worker.workspace/'response.json').read_text())
        responses.append(response)
        assert {entry['evaluation_id'] for entry in response['view']['entries']} == {None, 'validation'}
        assert response['result']['shared_attempt'] is not None
        assert 'later workspace edit' in (worker.workspace/'main.py').read_text()
        assert all('later workspace edit' not in FileWrite.from_dict(item).content().data.decode()
                   for item in response['files'])
    assert len(current.gateway('formal').store.pending()) == 0
    assert len(current.knowledge('research').store.snapshot().revisions) == 2
    execution = json.loads((tmp_path/'run/execution-0001.json').read_text())
    assert len(execution['rollouts']) == 5  # one private test and two full validation cohorts
    public_batches = execution['evaluations']
    assert len(public_batches) == 2
    assert sorted([row['score'] for row in batch['rows']] for batch in public_batches) == [[0, 0], [1, 1]]
    events = run_ledger(tmp_path/'run').events()
    assert run_recipe(recipe, registry=registry, lock=lock, root=tmp_path/'run', resume=True) == result
    assert run_ledger(tmp_path/'run').events() == events
    # Responses remain usable without restoring Values into this fresh service object.
    for response in responses:
        ticket = response['ticket']
        from proteus.gateway.records import GatewayTicket
        ticket = GatewayTicket.from_dict(ticket)
        assert current.gateway('formal').response(ticket.worker_id, ticket.id()) == response
