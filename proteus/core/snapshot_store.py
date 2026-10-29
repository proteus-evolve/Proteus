"""Content-addressed recovery snapshots with shared immutable JSON subtrees."""

from collections.abc import Mapping
import copy
import re
import sqlite3

from proteus.artifacts.store import _mkdir_durable, _sync_directory
from proteus.core.contracts import require
from proteus.core.serialization import Contract, canonical_json, content_hash, read_json


class SnapshotStore:
    FANOUT = 32

    def __init__(self, root):
        self.root = root
        self.path = root / 'objects.sqlite3'
        self.written = set()
        self.pending = {}
        self.immutable = {}
        self.atoms = {}

    def _put(self, node):
        digest = content_hash(node)
        if digest not in self.written:
            self.pending[digest] = canonical_json(node)
        return ['ref', digest]

    def _encode(self, value):
        if isinstance(value, Contract):
            cached = self.immutable.get(id(value))
            if cached is None:
                cached = self.immutable[id(value)] = (value, self._encode(value.to_dict()))
            return cached[1]
        if isinstance(value, Mapping):
            return self._put(['dict', [[key, self._encode(item)] for key, item in sorted(value.items())]])
        if isinstance(value, (list, tuple)):
            groups = [self._put(['list', [self._encode(item) for item in value[i:i + self.FANOUT]]])
                      for i in range(0, len(value), self.FANOUT)] or [self._put(['list', []])]
            while len(groups) > 1:
                groups = [self._put(['concat', groups[i:i + self.FANOUT]])
                          for i in range(0, len(groups), self.FANOUT)]
            return groups[0]
        if isinstance(value, str) and len(value) > 512:
            if value not in self.atoms:
                self.atoms[value] = self._put(['atom', value])
            return self.atoms[value]
        return ['value', value]

    def save(self, snapshot):
        reference = {'storage': 'merkle-v1', 'run_id': snapshot['run_id'],
                     'root': self._encode(snapshot)}
        _mkdir_durable(self.root)
        require(not self.path.is_symlink(), 'recovery object database cannot be a symlink')
        # One FULL-synchronous transaction per snapshot, rather than an fsync for
        # every shared object. Journal publication happens only after this commit.
        with sqlite3.connect(self.path) as db:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('CREATE TABLE IF NOT EXISTS objects (digest TEXT PRIMARY KEY, value TEXT NOT NULL)')
            db.executemany('INSERT OR IGNORE INTO objects VALUES (?,?)', self.pending.items())
        _sync_directory(self.root)
        self.written.update(self.pending)
        self.pending.clear()
        return reference

    def load(self, snapshot):
        # Existing journals remain readable; no silent rewriting of old evidence.
        if snapshot.get('storage') != 'merkle-v1':
            return snapshot
        cache = {}
        require(not self.path.is_symlink(), 'recovery object database cannot be a symlink')
        with sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True) as db:
            def decode(value):
                kind, body = value
                if kind == 'value':
                    return body
                require(kind == 'ref' and re.fullmatch(r'sha256:[0-9a-f]{64}', body),
                        'invalid recovery snapshot reference')
                if body not in cache:
                    row = db.execute('SELECT value FROM objects WHERE digest=?', (body,)).fetchone()
                    require(row is not None, 'recovery snapshot object is missing')
                    node = read_json(row[0])
                    require(content_hash(node) == body, 'recovery snapshot object identity differs')
                    tag, payload = node
                    if tag == 'dict':
                        result = {key: decode(item) for key, item in payload}
                    elif tag == 'list':
                        result = [decode(item) for item in payload]
                    elif tag == 'concat':
                        result = [item for part in payload for item in decode(part)]
                    else:
                        require(tag == 'atom', 'unknown recovery snapshot node')
                        result = payload
                    cache[body] = result
                return copy.deepcopy(cache[body])
            result = decode(snapshot['root'])
        require(result['run_id'] == snapshot['run_id'], 'recovery snapshot run identity differs')
        return result
