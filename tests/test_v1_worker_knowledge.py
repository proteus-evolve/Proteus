"""Worker permissions, concurrent CAS receipts, captured reads and actual exchange."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import subprocess
import sys
import urllib.error

import pytest

from proteus.artifacts.codecs import FileWrite
from proteus.core.contracts import DataRef
from proteus.core.serialization import ContractError
from proteus.core.serialization import content_hash
from proteus.examples import worker_knowledge
from proteus.gateway.knowledge import KnowledgeSubmission
from proteus.knowledge.client import KnowledgeClient
from proteus.recipes import load_recipe
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.runner import run_recipe
from test_v1_evaluation import SOURCE
from test_v1_gateway import services
from test_v1_ledger import run_ledger


def setup(root, access='read_write'):
    path = worker_knowledge.prepare(root/'study')
    registry = builtin_registry()
    worker_knowledge.register(registry)
    recipe = load_recipe(path)
    recipe = registry.resolve(replace(recipe, gateways=(replace(recipe.gateways[0], knowledge_access=access),)))
    lock = registry.make_lock(recipe, [SOURCE])
    (root/'recipe.json').write_text(recipe.to_json())
    (root/'lock.json').write_text(lock.to_json())
    return recipe, registry, lock


def client(gateway, worker):
    environment = gateway.connection(worker)
    return KnowledgeClient(environment['PROTEUS_EVALUATION_URL'], environment['PROTEUS_EVALUATION_TOKEN'])


def note(operation='first', *, expected=None, kind='notes', key='idea', value=b'first'):
    return KnowledgeSubmission(operation_id=operation, action='publish', kind=kind, key=key, expected=expected,
                               files=(FileWrite.from_bytes('SKILL.md' if kind == 'skills' else 'note.txt', value),))


@pytest.mark.parametrize('access', ['none', 'read', 'read_write'])
def test_knowledge_access_is_explicit_and_identity_cannot_be_overridden(tmp_path, access):
    recipe, registry, lock = setup(tmp_path, access)
    gateway = services(tmp_path, recipe, registry, lock).gateway('formal')
    with gateway.serve():
        a = client(gateway, 'a')
        if access == 'none':
            with pytest.raises(urllib.error.HTTPError):
                a.capture('initial')
        else:
            assert a.capture('initial')['snapshot']['revisions'] == []
        if access != 'read_write':
            with pytest.raises(urllib.error.HTTPError):
                a.write(note())
        else:
            response = a.write(note())
            assert response['record']['revision']['write']['writer_id'] == 'a'
            assert response['record']['revision']['write']['writer_role'] == 'proposer'
        for override in ({'writer_id':'b'}, {'writer_role':'framework'}, {'kind':'attempts'}):
            with pytest.raises(urllib.error.HTTPError):
                a._request('/v1/knowledge/writes', {**note('forged').to_dict(), **override})


def test_two_workers_cas_conflicts_reads_deletions_and_historical_receipts(tmp_path):
    recipe, registry, lock = setup(tmp_path)
    gateway = services(tmp_path, recipe, registry, lock).gateway('formal')
    with gateway.serve():
        a, b = client(gateway, 'a'), client(gateway, 'b')
        empty = a.capture('before')
        calls = [(a, note(value=b'a')), (b, note(value=b'b'))]
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda pair: pair[0].write(pair[1]), calls))
        assert sorted(item['outcome']['status'] for item in responses) == ['conflict', 'published']
        winner = next(item for item in responses if item['outcome']['status'] == 'published')
        original = DataRef.from_dict(winner['outcome']['revision'])
        saved = b.capture('after')
        assert saved['snapshot']['revisions'] == [original.to_dict()]
        updated = a.write(note('update', expected=original, value=b'updated'))
        second = DataRef.from_dict(updated['outcome']['revision'])
        assert second != original
        assert a.capture('before') == empty and b.capture('after') == saved
        assert b.read(original) == winner['record']
        for (c, request), response in zip(calls, responses):
            assert c.write(request) == response
        removed = b.write(KnowledgeSubmission(operation_id='delete', action='delete', kind='notes', key='idea', expected=second))
        tombstone = DataRef.from_dict(removed['outcome']['revision'])
        assert a.read(tombstone)['files'] == []
        restored = a.write(note('restore', expected=tombstone, value=b'restored'))
        assert restored['record']['revision']['revision'] == 4
        assert b.read(second)['files'][0]['base64'] == updated['record']['files'][0]['base64']
        with pytest.raises(urllib.error.HTTPError):
            a.write(note('update', expected=original, value=b'changed request'))


def test_missing_knowledge_database_cannot_reset_shared_history(tmp_path):
    recipe, registry, lock = setup(tmp_path)
    current = services(tmp_path, recipe, registry, lock)
    gateway = current.gateway('formal')
    with gateway.serve():
        client(gateway, 'a').write(note())
        current.knowledge('research').store.path.unlink()
        with pytest.raises(urllib.error.HTTPError):
            client(gateway, 'b').capture('after-deletion')
    with pytest.raises(ContractError, match='database is missing'):
        current.knowledge('research').store


def test_sources_cannot_import_private_values_or_another_runs_knowledge(tmp_path):
    recipe, registry, lock = setup(tmp_path)
    current = services(tmp_path, recipe, registry, lock)
    from proteus.artifacts.codecs import FileContent
    other = tmp_path/'other'
    other.mkdir()
    foreign_recipe, foreign_registry, foreign_lock = setup(other)
    foreign = services(other, foreign_recipe, foreign_registry, foreign_lock).knowledge('research').store.publish(
        'notes', 'foreign', {'note.txt': FileContent(b'foreign')}, writer_id='a', writer_role='proposer', operation_id='foreign')
    private = DataRef(kind='proteus.feedback@1', id=content_hash('private'), content_hash=content_hash('private'))
    gateway = current.gateway('formal')
    with gateway.serve():
        a = client(gateway, 'a')
        for ref in (foreign, private):
            with pytest.raises(urllib.error.HTTPError):
                a.write(replace(note('bad-source'), sources=(ref,)))
        assert a.capture('after')['snapshot']['revisions'] == []


def test_worker_operation_names_cannot_occupy_formal_attempt_namespace(tmp_path):
    recipe, registry, lock = setup(tmp_path)
    result = run_recipe(recipe, registry=registry, lock=lock, root=tmp_path/'run', artifact_source=tmp_path/'study/artifact-store')
    assert result.status.value == 'completed', result.diagnostic
    current = services(tmp_path, recipe, registry, lock)
    gateway = current.gateway('formal')
    workspace = current.worker('a').controller.workspace
    ticket = json.loads((workspace/'ticket.json').read_text())['ticket_id']
    outcome = gateway.knowledge().write('a', note('gateway:'+ticket, key='distinct-namespace'))
    assert outcome['outcome']['status'] == 'published'
    assert outcome['record']['revision']['write']['operation_id'].startswith('gateway-worker:')
    assert gateway.store.poll('a', ticket).result.shared_attempt is not None


def check_exchange(root, recipe, registry, lock):
    current = services(root, recipe, registry, lock)
    store = current.knowledge('research').store
    records = store.records(store.snapshot())
    assert sorted(record.write.kind for record in records) == ['attempts', 'attempts', 'notes', 'skills']
    note_record = next(record for record in records if record.write.kind == 'notes')
    skill = next(record for record in records if record.write.kind == 'skills')
    assert note_record.write.writer_id == 'a' and skill.write.writer_id == 'b'
    assert note_record.ref() in skill.write.sources
    assert len(skill.write.sources) == 2
    assert any(store.get(ref).write.kind == 'attempts' for ref in skill.write.sources)
    a, b = [current.worker(name).controller for name in ('a', 'b')]
    receipt = json.loads((b.workspace/'strategy.json').read_text())
    assert receipt['record']['ref'] == note_record.ref().to_dict()
    assert all(len(list(worker.private.glob('*/request.json'))) == 1 for worker in (a, b))
    execution = json.loads((root/'run/execution-0001.json').read_text())
    assert sorted([row['score'] for row in batch['rows']] for batch in execution['evaluations']) == [[0, 0], [1, 1]]
    book = run_ledger(root/'run')
    assert book.balance()['charged']['evaluations'] == 5 and book.balance()['active_slots'] == 0
    return book


def test_actual_workers_exchange_note_then_publish_skill_with_formal_attempt_source(tmp_path):
    recipe, registry, lock = setup(tmp_path)
    result = run_recipe(recipe, registry=registry, lock=lock, root=tmp_path/'run', artifact_source=tmp_path/'study/artifact-store')
    assert result.status.value == 'completed', result.diagnostic
    book = check_exchange(tmp_path, recipe, registry, lock)
    before = book.events()
    assert run_recipe(recipe, registry=registry, lock=lock, root=tmp_path/'run', resume=True) == result
    assert book.events() == before


WORKER = r'''
import os,sys
from pathlib import Path
from proteus.examples.worker_knowledge import register
from proteus.knowledge.store import SharedKnowledgeStore
from proteus.recipes import load_recipe
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.manifest import DependencyLock
from proteus.recipes.runner import run_recipe
root,boundary=Path(sys.argv[1]),sys.argv[2]
registry=builtin_registry(); register(registry)
recipe=load_recipe(root/'recipe.json')
lock=DependencyLock.from_json((root/'lock.json').read_text())
original=SharedKnowledgeStore._publish if boundary=='write' else SharedKnowledgeStore.snapshot
def interrupted(self,*args,**kwargs):
    result=original(self,*args,**kwargs)
    os._exit(86)
setattr(SharedKnowledgeStore,'_publish' if boundary=='write' else 'snapshot',interrupted)
run_recipe(recipe,registry=registry,lock=lock,root=root/'run',artifact_source=root/'study/artifact-store')
'''


@pytest.mark.parametrize('boundary', ['write', 'capture'])
def test_controller_exit_after_worker_knowledge_commit_preserves_exchange(tmp_path, boundary):
    recipe, registry, lock = setup(tmp_path)
    stopped = subprocess.run([sys.executable, '-c', WORKER, str(tmp_path), boundary], capture_output=True, text=True, timeout=35)
    assert stopped.returncode == 86, stopped.stdout+stopped.stderr
    result = run_recipe(recipe, registry=registry, lock=lock, root=tmp_path/'run', resume=True)
    assert result.status.value == 'completed', result.diagnostic
    check_exchange(tmp_path, recipe, registry, lock)
