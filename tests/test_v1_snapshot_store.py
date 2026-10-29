"""Snapshot sharing preserves exact state and verifies every recovered object."""

import sqlite3

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
