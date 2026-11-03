"""Admission closure is atomic with uploads; queued cancellation never invents a trial."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import threading

import pytest

from proteus.core.serialization import ContractError
from proteus.gateway.records import GatewayResult
from test_v1_gateway import services, setup, submission


def test_closure_preserves_pending_snapshot_retries_and_unexecuted_outcomes(tmp_path):
    recipe, registry, lock = setup(tmp_path)
    current = services(tmp_path, recipe, registry, lock)
    gateway = current.gateway('formal')
    first = gateway.submit('a', submission())
    closure = gateway.close('stop', 'submission_limit')
    assert closure.pending == (first,)
    assert gateway.close('stop', 'submission_limit') == closure
    assert gateway.submit('a', submission()) == first
    with pytest.raises(ContractError, match='closed'):
        gateway.submit('b', submission())
    with pytest.raises(ContractError, match='reason changed'):
        gateway.close('stop', 'requested')
    result = gateway.cancel(first, closure)
    assert result.status == 'failed' and result.candidate is result.evidence is result.shared_attempt is None
    assert 'was not dispatched' in result.diagnostic
    assert gateway.store.poll('a', first.id()).result == result
    assert gateway.cancel(first, closure) == result and not gateway.store.pending()
    with pytest.raises(ContractError, match='must be claimed'):
        gateway.process(first)
    assert not current.ledger.entries() and current.rollouts == []
    assert gateway.store.closure().pending == (first,)  # Historical close snapshot does not shrink.
    from proteus.gateway.service import validate_restored
    validate_restored(current)


def test_inflight_upload_cannot_admit_after_close_commits(tmp_path, monkeypatch):
    recipe, registry, lock = setup(tmp_path)
    gateway = services(tmp_path, recipe, registry, lock).gateway('formal')
    store = gateway.store
    original = store.preflight
    ready, proceed = threading.Event(), threading.Event()

    def preflight(*args):
        value = original(*args)
        ready.set()
        assert proceed.wait(5)
        return value

    monkeypatch.setattr(store, 'preflight', preflight)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(gateway.submit, 'a', submission())
        assert ready.wait(5)
        try:
            closure = gateway.close('stop', 'requested')
        finally:
            proceed.set()
        with pytest.raises(ContractError, match='closed'):
            pending.result(timeout=5)
    assert not closure.pending and not store.pending() and not store.has_requests()


def test_claimed_or_completed_evaluations_cannot_be_cancelled(tmp_path):
    recipe, registry, lock = setup(tmp_path)
    current = services(tmp_path, recipe, registry, lock)
    gateway = current.gateway('formal')
    ticket = gateway.submit('a', submission())
    assert gateway.claim('take') == ticket
    closure = gateway.close('stop', 'requested')
    with pytest.raises(ContractError, match='require reconciliation'):
        gateway.cancel(ticket, closure)
    completed = gateway.store.finish(GatewayResult(ticket=ticket, status='failed', diagnostic='formal evaluator failed'))
    with pytest.raises(ContractError, match='cannot be replaced'):
        gateway.cancel(ticket, closure)
    assert gateway.store.poll('a', ticket.id()).result == completed
    with pytest.raises(ContractError, match='saved closure'):
        gateway.store.cancel(ticket, replace(closure, operation_id='forged'))
