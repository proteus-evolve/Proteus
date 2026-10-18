"""Shared revisions remain atomic across writers, crashes and read snapshots."""

from dataclasses import replace
import json
import subprocess
import sys

import pytest

from proteus.artifacts.codecs import FileContent, bytes_hash
from proteus.core.contracts import DataRef
from proteus.core.serialization import ContractError, content_hash
from proteus.evidence.records import EvidenceAccess, EvidenceEntry, EvidenceView
from proteus.evidence.store import EvidenceStore
from proteus.knowledge import KnowledgeConflict, KnowledgeIdentity, SharedKnowledgeStore
from proteus.recipes.builtins import builtin_registry


IDENTITY = KnowledgeIdentity(run_id="knowledge-fixture", config_hash=content_hash("recipe"), lock_hash=None,
    evidence_access=EvidenceAccess(evaluation_ids=("train",), splits=("train",), kinds=("evaluation",)))


def store(root, **kwargs):
    return SharedKnowledgeStore(root, builtin_registry(), identity=IDENTITY, **kwargs)


def publish(knowledge, text, *, writer="worker-1", operation="note", expected=None, key="research/result", sources=()):
    return knowledge.publish("notes", key, {"note.md": FileContent(text.encode())}, writer_id=writer,
                             operation_id=operation, expected=expected, sources=sources)


def test_revision_sources_conflicts_deletion_and_historical_exports(tmp_path):
    knowledge = store(tmp_path / "knowledge")
    first = publish(knowledge, "first")
    snapshot = knowledge.snapshot()
    second = publish(knowledge, "second", writer="worker-2", expected=first, sources=(first,))
    assert knowledge.get(second).write.sources == (first,)
    assert [record.ref() for record in knowledge.history("notes", "research/result")] == [first, second]
    with pytest.raises(KnowledgeConflict) as error:
        publish(knowledge, "stale", writer="worker-3", expected=first)
    assert error.value.expected == first and error.value.current == second
    assert publish(knowledge, "first") == first  # Replay after a later write returns the original receipt.
    with pytest.raises(ContractError, match="different inputs"):
        publish(knowledge, "changed request")
    deleted = knowledge.delete("notes", "research/result", writer_id="worker-2", operation_id="delete", expected=second)
    assert knowledge.head("notes", "research/result") == deleted
    assert len(knowledge.history("notes", "research/result")) == 3
    with pytest.raises(ContractError, match="deletion"):
        knowledge.read(deleted, "note.md")
    historical = knowledge.materialize(snapshot, tmp_path / "old")
    assert (historical / "notes/research/result/note.md").read_text() == "first"
    assert not (knowledge.materialize(knowledge.snapshot(), tmp_path / "new") / "notes").exists()
    # An export is a copy. Editing it does not change historical evidence.
    edited = historical / "notes/research/result/note.md"
    edited.chmod(0o644)
    edited.write_text("local edit")
    assert knowledge.read(first, "note.md") == b"first"
    restored = publish(knowledge, "restored", writer="worker-3", operation="restore", expected=deleted)
    assert store(tmp_path / "knowledge").head("notes", "research/result") == restored
    assert knowledge.get(restored).revision == 4


def test_skills_keep_complete_files_and_executable_metadata(tmp_path):
    knowledge = store(tmp_path / "knowledge")
    files = {"SKILL.md": FileContent(b"# Reuse\nRun script.py"), "script.py": FileContent(b"print(1)\n", True)}
    ref = knowledge.publish("skills", "solver", files, writer_id="worker-1", operation_id="skill")
    exported = knowledge.materialize(knowledge.snapshot(), tmp_path / "export")
    assert knowledge.read(ref, "script.py") == b"print(1)\n"
    assert (exported / "skills/solver/script.py").stat().st_mode & 0o111
    with pytest.raises(ContractError, match="SKILL.md"):
        knowledge.publish("skills", "invalid", {"script.py": files["script.py"]},
                          writer_id="worker-1", operation_id="invalid")


@pytest.mark.parametrize("key", ["../escape", "a/../b", "/absolute", "a\\b", "a/CON", "a/", "a\x00b"])
def test_keys_cannot_escape_or_alias_store_paths(tmp_path, key):
    knowledge = store(tmp_path / "knowledge")
    with pytest.raises(ContractError):
        publish(knowledge, "unsafe", key=key)
    assert knowledge.snapshot().revisions == ()


@pytest.mark.parametrize("key", ["RESEARCH/result", "research/result/child", "research", "Research/other"])
def test_distinct_keys_do_not_collide_in_materialized_filesystem(tmp_path, key):
    knowledge = store(tmp_path / "knowledge")
    first = publish(knowledge, "first")
    with pytest.raises(ContractError, match="collide or overlap"):
        publish(knowledge, "collision", operation="conflict", key=key)
    assert knowledge.snapshot().revisions == (first,)


def test_shared_directory_unicode_aliases_are_rejected_before_publication(tmp_path):
    knowledge = store(tmp_path / "knowledge")
    first = publish(knowledge, "first", key="caf\u00e9/a")
    with pytest.raises(ContractError, match="collide or overlap"):
        publish(knowledge, "other", operation="other", key="cafe\u0301/b")
    assert knowledge.snapshot().revisions == (first,)


def evidence(root, identity=IDENTITY):
    evidence_store = EvidenceStore(root)
    payload = b'{"score": 0.5}'
    view = EvidenceView(run_id=identity.run_id, config_hash=identity.config_hash, lock_hash=identity.lock_hash,
        stage_id="publish", role="researcher", access=EvidenceAccess(evaluation_ids=("train",),
            splits=("train",), kinds=("evaluation",)), candidate_ids=("candidate-1",),
        entries=(EvidenceEntry(path="scores/result.json", kind="evaluation", candidate_id="candidate-1",
            content_hash=bytes_hash(payload), size_bytes=len(payload), source_hash=content_hash("cohort"),
            evaluation_id="train", operation_id="evaluation-1", split="train"),))
    return evidence_store, evidence_store.put(view, {"scores/result.json": payload})


def test_formal_attempts_copy_released_evidence_and_cannot_be_forged_by_note_writes(tmp_path):
    evidence_store, view = evidence(tmp_path / "evidence")
    knowledge = store(tmp_path / "knowledge", evidence=evidence_store)
    attempt = knowledge.record_attempt("trial-1", view, writer_id="worker-1", operation_id="evaluation-1")
    assert knowledge.read(attempt, "evidence/scores/result.json") == b'{"score": 0.5}'
    assert knowledge.get(attempt).write.sources == (view,)
    assert knowledge.record_attempt("trial-1", view, writer_id="worker-1", operation_id="evaluation-1") == attempt
    note = publish(knowledge, "observed improvement", sources=(attempt, view))
    assert knowledge.get(note).write.sources == (attempt, view)
    with pytest.raises(ContractError, match="formal attempts"):
        knowledge.publish("attempts", "forged", {"score.json": FileContent(b"1")},
                          writer_id="worker-2", operation_id="forged")
    with pytest.raises(ContractError, match="cannot be deleted"):
        knowledge.delete("attempts", "trial-1", writer_id="worker-1", operation_id="delete", expected=attempt)
    with pytest.raises(KnowledgeConflict):
        knowledge.record_attempt("trial-1", view, writer_id="worker-2", operation_id="other-evaluation")
    private_ref = DataRef(kind="proteus.eval.receipt@1", id="private", content_hash=content_hash("hidden"))
    with pytest.raises(ContractError, match="released evidence"):
        publish(knowledge, "private", operation="private", sources=(private_ref,))


def test_run_identity_and_cross_run_sources_are_rejected(tmp_path):
    store(tmp_path / "knowledge")
    with pytest.raises(ContractError, match="run/configuration/lock"):
        SharedKnowledgeStore(tmp_path / "knowledge", builtin_registry(), identity=replace(IDENTITY, run_id="other"))
    foreign_store, foreign_ref = evidence(tmp_path / "evidence", replace(IDENTITY, run_id="other"))
    knowledge = store(tmp_path / "knowledge", evidence=foreign_store)
    with pytest.raises(ContractError, match="another run"):
        publish(knowledge, "foreign", sources=(foreign_ref,))


def test_publication_cannot_widen_shared_evidence_permissions(tmp_path):
    evidence_store, view = evidence(tmp_path / "evidence")
    knowledge = SharedKnowledgeStore(tmp_path / "knowledge", builtin_registry(), evidence=evidence_store,
                                     identity=replace(IDENTITY, evidence_access=None))
    with pytest.raises(ContractError, match="disclosure policy"):
        knowledge.record_attempt("trial", view, writer_id="worker", operation_id="evaluation")
    with pytest.raises(ContractError, match="run/configuration/lock"):
        store(tmp_path / "knowledge", evidence=evidence_store)


def test_snapshot_must_be_an_actual_atomic_capture(tmp_path):
    knowledge = store(tmp_path / "knowledge")
    first = publish(knowledge, "first")
    snapshot = knowledge.snapshot()
    second = publish(knowledge, "second", operation="second", expected=first)
    invented = replace(snapshot, revisions=(second,))
    with pytest.raises(ContractError, match="unknown knowledge snapshot"):
        knowledge.materialize(invented, tmp_path / "invented")
    assert knowledge.get_snapshot(snapshot.ref()) == snapshot
    assert knowledge.snapshot() == invented
    assert knowledge.records(invented)[0].ref() == second


def test_conflict_and_captured_read_retries_keep_original_observations(tmp_path):
    knowledge = store(tmp_path / "knowledge")
    first = publish(knowledge, "first")
    observed = knowledge.snapshot(reader_id="worker-2", operation_id="read")
    second = publish(knowledge, "second", operation="second", expected=first)
    with pytest.raises(KnowledgeConflict) as original:
        publish(knowledge, "stale", writer="worker-2", expected=first)
    third = publish(knowledge, "third", operation="third", expected=second)
    with pytest.raises(KnowledgeConflict) as retried:
        publish(store(tmp_path / "knowledge"), "stale", writer="worker-2", expected=first)
    assert retried.value.current == original.value.current == second
    assert knowledge.snapshot(reader_id="worker-2", operation_id="read") == observed
    assert knowledge.snapshot().revisions == (third,)


WORKER = r"""
import json, os, sys, time
from contextlib import contextmanager
from pathlib import Path
from proteus.artifacts.codecs import FileContent
from proteus.core.contracts import DataRef
from proteus.knowledge import KnowledgeIdentity, SharedKnowledgeStore, KnowledgeConflict
from proteus.recipes.builtins import builtin_registry
root, mode, name = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
config = json.loads((root / 'worker.json').read_text())
knowledge = SharedKnowledgeStore(root / 'knowledge', builtin_registry(), identity=KnowledgeIdentity.from_dict(config['identity']))
expected = DataRef.from_dict(config['expected'])
if mode == 'before_commit':
    original = knowledge._transaction
    @contextmanager
    def interrupted(*, write=False):
        with original(write=write) as db:
            yield db
            if write: os._exit(86)
    knowledge._transaction = interrupted
if mode == 'race':
    (root / (name + '.ready')).write_text('ready')
    deadline = time.monotonic() + 20
    while not (root / 'go').exists():
        if time.monotonic() > deadline: raise RuntimeError('test barrier timed out')
        time.sleep(0.01)
try:
    result = knowledge.publish('notes', 'shared', {'a.txt': FileContent(name.encode()), 'b.txt': FileContent(name.encode())},
        writer_id=name, operation_id='write', expected=expected)
    if mode == 'after_commit': os._exit(86)
    print(result.to_json())
except KnowledgeConflict:
    print('conflict')
"""


def concurrent_setup(root):
    knowledge = store(root / "knowledge")
    first = knowledge.publish("notes", "shared", {"a.txt": FileContent(b"first"), "b.txt": FileContent(b"first")},
                              writer_id="initial", operation_id="write")
    (root / "worker.json").write_text(json.dumps({"identity": IDENTITY.to_dict(), "expected": first.to_dict()}))
    return knowledge, first


def test_two_processes_cannot_overwrite_the_same_observed_revision(tmp_path):
    import time

    knowledge, first = concurrent_setup(tmp_path)
    children = [subprocess.Popen([sys.executable, "-c", WORKER, str(tmp_path), "race", name],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for name in ("one", "two")]
    try:
        deadline = time.monotonic() + 20
        while not all((tmp_path / (name + ".ready")).exists() for name in ("one", "two")):
            assert all(child.poll() is None for child in children)
            assert time.monotonic() < deadline
            time.sleep(0.01)
        (tmp_path / "go").write_text("go")
        outputs = [child.communicate(timeout=30) for child in children]
        assert all(child.returncode == 0 for child in children), outputs
        assert sorted(output[0].strip() == "conflict" for output in outputs) == [False, True]
        history = knowledge.history("notes", "shared")
        assert len(history) == 2 and history[0].ref() == first
        last = history[-1].ref()
        assert knowledge.read(last, "a.txt") == knowledge.read(last, "b.txt")
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait()


@pytest.mark.parametrize("boundary", ["before_commit", "after_commit"])
def test_hard_exit_keeps_atomic_publication_and_idempotent_retry(tmp_path, boundary):
    knowledge, first = concurrent_setup(tmp_path)
    stopped = subprocess.run([sys.executable, "-c", WORKER, str(tmp_path), boundary, "worker"],
                             text=True, capture_output=True, timeout=30)
    assert stopped.returncode == 86, stopped.stdout + stopped.stderr
    head = knowledge.head("notes", "shared")
    assert (head == first) == (boundary == "before_commit")
    completed = subprocess.run([sys.executable, "-c", WORKER, str(tmp_path), "retry", "worker"],
                               text=True, capture_output=True, timeout=30)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = DataRef.from_json(completed.stdout)
    assert knowledge.read(result, "a.txt") == knowledge.read(result, "b.txt") == b"worker"
    assert len(knowledge.history("notes", "shared")) == 2
    if boundary == "after_commit":
        assert result == head


def test_corrupt_content_cannot_be_read_or_exported(tmp_path):
    knowledge = store(tmp_path / "knowledge")
    ref = publish(knowledge, "original")
    record = knowledge.get(ref)
    path = knowledge.artifacts._path("blobs", record.write.artifact.components[0].content_hash)
    path.chmod(0o644)
    path.write_bytes(b"corruption")
    with pytest.raises(ContractError):
        knowledge.read(ref, "note.md")
    with pytest.raises(ContractError):
        knowledge.materialize(knowledge.snapshot(), tmp_path / "export")
    assert not (tmp_path / "export").exists()
