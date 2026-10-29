"""Snapshot sharing preserves exact state and verifies every recovered object."""

import sqlite3
import subprocess
import sys

import pytest

from proteus.core.snapshot_store import SnapshotStore
from proteus.core.serialization import ContractError


def test_shared_snapshots_restore_old_and_new_states_and_detect_corruption(tmp_path):
    store = SnapshotStore(tmp_path)
    first = {'run_id': 'study', 'records': [{'text': 'private' * 1000, 'n': i} for i in range(80)]}
    old = store.save(first)
    second = {**first, 'records': [*first['records'], {'text': 'new evidence', 'n': 80}]}
    new = store.save(second)
    assert SnapshotStore(tmp_path).load(old) == first
    assert SnapshotStore(tmp_path).load(new) == second
    assert store.load(first) == first  # Legacy full snapshots remain readable.
    loaded = store.load(old)
    loaded['records'][0]['text'] = 'mutated outside the store'
    assert store.load(old) == first
    with sqlite3.connect(store.path) as db:
        db.execute('UPDATE objects SET value=? WHERE digest=?', ('["atom","changed"]', old['root'][1]))
    with pytest.raises(ContractError, match='identity differs'):
        store.load(old)


def test_missing_snapshot_objects_cannot_be_replaced_by_partial_state(tmp_path):
    store = SnapshotStore(tmp_path)
    reference = store.save({'run_id': 'study', 'records': [1, 2, 3]})
    with sqlite3.connect(store.path) as db:
        db.execute('DELETE FROM objects WHERE digest=?', (reference['root'][1],))
    with pytest.raises(ContractError, match='object is missing'):
        store.load(reference)


def test_load_recovers_hot_journal_without_exposing_uncommitted_objects(tmp_path):
    store = SnapshotStore(tmp_path)
    committed = {'run_id': 'study', 'records': [{'text': 'committed', 'n': 1}]}
    reference = store.save(committed)
    # A small pager cache forces uncommitted pages onto disk before hard exit.
    # The published snapshot still refers to the preceding committed transaction.
    crashed = subprocess.run([sys.executable, '-c', '''
import os,sqlite3,sys
db=sqlite3.connect(sys.argv[1])
db.execute('PRAGMA cache_size=1')
db.execute('BEGIN IMMEDIATE')
db.execute('UPDATE objects SET value=? WHERE digest=?', ('uncommitted corruption', sys.argv[2]))
db.executemany('INSERT INTO objects VALUES (?,?)',
               ((f'uncommitted-{i}', 'x'*8192) for i in range(64)))
os._exit(86)
''', str(store.path), reference['root'][1]], capture_output=True, text=True, timeout=15)
    assert crashed.returncode == 86, crashed.stdout + crashed.stderr
    journal = store.path.with_name(store.path.name + '-journal')
    assert journal.stat().st_size > 512
    restored = SnapshotStore(tmp_path)
    assert restored.load(reference) == committed
    assert restored.load(reference) == committed
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM objects WHERE digest LIKE 'uncommitted-%'").fetchone()[0] == 0
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    next_state = {**committed, 'records': [*committed['records'], {'text': 'after recovery', 'n': 2}]}
    next_reference = restored.save(next_state)
    assert restored.load(reference) == committed
    assert restored.load(next_reference) == next_state


def test_loading_missing_database_does_not_create_an_empty_replacement(tmp_path):
    store = SnapshotStore(tmp_path)
    reference = store.save({'run_id': 'study', 'records': [1]})
    store.path.unlink()
    with pytest.raises(sqlite3.OperationalError):
        SnapshotStore(tmp_path).load(reference)
    assert not store.path.exists()
