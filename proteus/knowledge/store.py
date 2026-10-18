"""Atomic compare-and-swap publication with immutable history and retry identity.

The controller owns this store. Workers receive exported files and a mediated
publication interface, never the private database. Writer IDs are supplied by the
authenticated caller; this local storage API does not authenticate processes.
"""

from contextlib import contextmanager
from pathlib import Path
import shutil
import sqlite3

from proteus.artifacts.codecs import FileContent, components
from proteus.artifacts.store import ArtifactStore, _mkdir_durable, _sync_directory
from proteus.core.contracts import PluginRef, require
from proteus.core.serialization import ContractError, canonical_json, content_hash
from proteus.knowledge.records import (
    REVISION_KIND, SNAPSHOT_KIND, KnowledgeIdentity, KnowledgeOutcome, KnowledgeRevision, KnowledgeSnapshot, KnowledgeWrite,
    revision_ref, validate_key,
)

FILE_TREE = PluginRef(name="proteus.file_tree", version="1")


class KnowledgeConflict(ContractError):
    def __init__(self, expected, current):
        super().__init__("shared knowledge changed since the supplied revision")
        self.expected, self.current = expected, current


class SharedKnowledgeStore:
    def __init__(self, root, registry, *, identity: KnowledgeIdentity, evidence=None):
        self.root = Path(root).resolve()
        _mkdir_durable(self.root)
        self.path = self.root / "knowledge.sqlite3"
        require(not self.path.is_symlink(), "knowledge database cannot be a symlink")
        self.identity, self.evidence = identity, evidence
        self.artifacts = ArtifactStore(self.root / "artifacts", registry, allowed_codecs=(FILE_TREE,))
        with self._transaction(write=True) as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            require(version in (0, 1), "unsupported knowledge schema")
            if version == 0:
                require(not tables, "refusing to initialize an unrecognized knowledge database")
                for sql in (
                    "CREATE TABLE identity (value TEXT NOT NULL)",
                    "CREATE TABLE revisions (digest TEXT PRIMARY KEY, kind TEXT NOT NULL, key TEXT NOT NULL, "
                    "revision INTEGER NOT NULL, body TEXT NOT NULL, UNIQUE(kind,key,revision))",
                    "CREATE TABLE heads (kind TEXT NOT NULL, key TEXT NOT NULL, digest TEXT NOT NULL "
                    "REFERENCES revisions(digest), PRIMARY KEY(kind,key))",
                    "CREATE TABLE operations (role TEXT NOT NULL, writer TEXT NOT NULL, operation TEXT NOT NULL, request TEXT NOT NULL, "
                    "outcome TEXT NOT NULL, PRIMARY KEY(role,writer,operation))",
                    "CREATE TABLE snapshots (digest TEXT PRIMARY KEY, body TEXT NOT NULL)",
                    "CREATE TABLE reads (reader TEXT NOT NULL, operation TEXT NOT NULL, digest TEXT NOT NULL "
                    "REFERENCES snapshots(digest), PRIMARY KEY(reader,operation))",
                ):
                    db.execute(sql)
                db.execute("INSERT INTO identity VALUES (?)", (canonical_json(identity),))
                db.execute("PRAGMA user_version=1")
            else:
                require(tables == {"identity", "revisions", "heads", "operations", "snapshots", "reads"}, "knowledge schema differs")
                rows = db.execute("SELECT value FROM identity").fetchall()
                require(len(rows) == 1 and rows[0][0] == canonical_json(identity), "knowledge run/configuration/lock differs")
        _sync_directory(self.root)

    @contextmanager
    def _transaction(self, *, write=False):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    def _revision(self, db, digest):
        row = db.execute("SELECT * FROM revisions WHERE digest=?", (digest,)).fetchone()
        require(row is not None, "unknown knowledge revision")
        record = KnowledgeRevision.from_json(row["body"])
        require(record.ref().content_hash == digest and record.identity_hash == content_hash(self.identity)
                and (record.write.kind, record.write.key, record.revision) == (row["kind"], row["key"], row["revision"]),
                "knowledge revision integrity mismatch")
        return record

    def get(self, ref):
        revision_ref(ref)
        with self._transaction() as db:
            record = self._revision(db, ref.content_hash)
        require(record.ref() == ref, "knowledge reference differs")
        if record.write.artifact is not None:
            self.artifacts.get(record.write.artifact)
        return record

    def _head(self, db, kind, key):
        row = db.execute("SELECT digest FROM heads WHERE kind=? AND key=?", (kind, key)).fetchone()
        return self._revision(db, row[0]).ref() if row else None

    def head(self, kind, key):
        self._key(kind, key)
        with self._transaction() as db:
            return self._head(db, kind, key)

    def history(self, kind, key):
        self._key(kind, key)
        with self._transaction() as db:
            records = tuple(self._revision(db, row[0]) for row in db.execute(
                "SELECT digest FROM revisions WHERE kind=? AND key=? ORDER BY revision", (kind, key)))
        previous = None
        for number, record in enumerate(records, 1):
            require(record.revision == number and record.write.expected == previous, "knowledge history has a gap")
            previous = record.ref()
        return records

    @staticmethod
    def _key(kind, key):
        require(kind in ("notes", "skills", "attempts"), "unknown knowledge kind")
        validate_key(key)

    def _view(self, ref):
        require(self.evidence is not None and ref.kind == "proteus.evidence.view@1", "source is not released evidence")
        view = self.evidence.verify(ref)
        require((view.run_id, view.config_hash, view.lock_hash) == (
            self.identity.run_id, self.identity.config_hash, self.identity.lock_hash), "evidence belongs to another run")
        access = self.identity.evidence_access
        require(access is not None and set(view.access.evaluation_ids) <= set(access.evaluation_ids)
                and set(view.access.splits) <= set(access.splits) and set(view.access.kinds) <= set(access.kinds),
                "evidence exceeds the shared knowledge disclosure policy")
        return view

    def _sources(self, sources):
        for ref in sources:
            if ref.kind == REVISION_KIND:
                self.get(ref)
            else:
                self._view(ref)

    def publish(self, kind, key, files, *, writer_id, operation_id, expected=None, sources=(), writer_role=None):
        require(kind in ("notes", "skills"), "workers publish notes/skills; formal attempts require released evidence")
        require(bool(files), "knowledge publication needs files")
        if kind == "skills":
            require("SKILL.md" in files, "a skill requires SKILL.md")
        artifact = self.artifacts.put(FILE_TREE, files)
        return self._publish(KnowledgeWrite(kind=kind, key=key, writer_id=writer_id, operation_id=operation_id,
            expected=expected, artifact=artifact, sources=tuple(sources), writer_role=writer_role))

    def delete(self, kind, key, *, writer_id, operation_id, expected, sources=(), writer_role=None):
        require(kind in ("notes", "skills"), "formal attempts cannot be deleted")
        return self._publish(KnowledgeWrite(kind=kind, key=key, writer_id=writer_id, operation_id=operation_id,
            expected=expected, artifact=None, sources=tuple(sources), writer_role=writer_role))

    def record_attempt(self, key, evidence, *, writer_id, operation_id, writer_role=None):
        """Controller records already-released evidence, never a worker's claimed score."""
        view = self._view(evidence)
        require(len(view.candidate_ids) == 1 and any(row.kind == "evaluation" for row in view.entries),
                "formal attempt needs one candidate and released evaluation evidence")
        files = {"view.json": FileContent(view.to_json().encode())}
        files.update({"evidence/" + row.path: FileContent(self.evidence.read(evidence, row.path)) for row in view.entries})
        artifact = self.artifacts.put(FILE_TREE, files)
        return self._publish(KnowledgeWrite(kind="attempts", key=key, writer_id=writer_id, operation_id=operation_id,
            expected=None, artifact=artifact, sources=(evidence,), writer_role=writer_role))

    def _publish(self, write):
        self._sources(write.sources)
        request_hash = content_hash(write)
        with self._transaction(write=True) as db:
            prior = db.execute("SELECT request,outcome FROM operations WHERE role=? AND writer=? AND operation=?",
                               (write.writer_role or "", write.writer_id, write.operation_id)).fetchone()
            if prior is not None:
                require(prior[0] == request_hash, "knowledge operation was reused with different inputs")
                outcome = KnowledgeOutcome.from_json(prior[1])
                if outcome.revision is not None:
                    require(self._revision(db, outcome.revision.content_hash).write == write,
                            "knowledge operation receipt differs from its revision")
            else:
                current = self._head(db, write.kind, write.key)
                if current != write.expected:
                    outcome = KnowledgeOutcome(status="conflict", current=current)
                else:
                    keys = {row[0]: FileContent(b"") for row in db.execute(
                        "SELECT key FROM heads WHERE kind=?", (write.kind,))}
                    keys[write.key] = FileContent(b"")
                    try:
                        # The artifact path contract also checks directory prefixes:
                        # Research/a and research/b must not alias a shared folder.
                        components(keys)
                    except ContractError as exc:
                        raise ContractError("knowledge keys collide or overlap") from exc
                    previous = self._revision(db, current.content_hash) if current else None
                    record = KnowledgeRevision(identity_hash=content_hash(self.identity),
                        revision=previous.revision + 1 if previous else 1, write=write)
                    ref = record.ref()
                    db.execute("INSERT INTO revisions VALUES (?,?,?,?,?)",
                               (ref.content_hash, write.kind, write.key, record.revision, record.to_json()))
                    db.execute("INSERT INTO heads VALUES (?,?,?) ON CONFLICT(kind,key) DO UPDATE SET digest=excluded.digest",
                               (write.kind, write.key, ref.content_hash))
                    outcome = KnowledgeOutcome(status="published", revision=ref)
                db.execute("INSERT INTO operations VALUES (?,?,?,?,?)",
                           (write.writer_role or "", write.writer_id, write.operation_id, request_hash, outcome.to_json()))
        if outcome.status == "conflict":
            raise KnowledgeConflict(write.expected, outcome.current)
        return outcome.revision

    def snapshot(self, *, reader_id=None, operation_id=None):
        require((reader_id is None and operation_id is None) or all(type(value) is str and value.strip()
                for value in (reader_id, operation_id)), "recorded reads need reader and operation identities")
        with self._transaction(write=True) as db:
            if reader_id is not None:
                prior = db.execute("SELECT digest FROM reads WHERE reader=? AND operation=?", (reader_id, operation_id)).fetchone()
                if prior is not None:
                    return self._snapshot(db, prior[0])
            refs = tuple(self._revision(db, row[0]).ref() for row in db.execute("SELECT digest FROM heads ORDER BY kind,key"))
            snapshot = KnowledgeSnapshot(identity=self.identity, revisions=refs)
            db.execute("INSERT OR IGNORE INTO snapshots VALUES (?,?)", (content_hash(snapshot), snapshot.to_json()))
            if reader_id is not None:
                db.execute("INSERT INTO reads VALUES (?,?,?)", (reader_id, operation_id, content_hash(snapshot)))
        return snapshot

    def _snapshot(self, db, digest):
        row = db.execute("SELECT body FROM snapshots WHERE digest=?", (digest,)).fetchone()
        require(row is not None, "unknown knowledge snapshot")
        snapshot = KnowledgeSnapshot.from_json(row[0])
        require(content_hash(snapshot) == digest and snapshot.identity == self.identity, "knowledge snapshot integrity mismatch")
        return snapshot

    def get_snapshot(self, ref):
        require(ref.kind == SNAPSHOT_KIND and ref.id == content_hash([SNAPSHOT_KIND, ref.content_hash]),
                "not a knowledge snapshot reference")
        with self._transaction() as db:
            return self._snapshot(db, ref.content_hash)

    def records(self, snapshot):
        require(snapshot.identity == self.identity, "knowledge snapshot belongs to another run")
        require(self.get_snapshot(snapshot.ref()) == snapshot, "knowledge snapshot differs from its publication")
        records = tuple(self.get(ref) for ref in snapshot.revisions)
        keys = [(item.write.kind, item.write.key) for item in records]
        require(keys == sorted(set(keys)), "snapshot keys must be unique and ordered")
        return records

    def read(self, ref, path):
        record = self.get(ref)
        require(record.write.artifact is not None, "knowledge revision is a deletion")
        files = self.artifacts.get(record.write.artifact)
        require(path in files, "file is outside the knowledge revision")
        return files[path].data

    def materialize(self, snapshot, destination):
        files = {"knowledge.json": FileContent(snapshot.to_json().encode())}
        for record in self.records(snapshot):
            if record.write.artifact is not None:
                files.update({f"{record.write.kind}/{record.write.key}/{path}": value
                              for path, value in self.artifacts.get(record.write.artifact).items()})
        components(files)
        destination = Path(destination)
        require(not destination.exists() and not destination.is_symlink(), "knowledge export requires a fresh destination")
        destination.mkdir(parents=True)
        try:
            for name, item in files.items():
                target = destination / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(item.data)
                target.chmod(0o555 if item.executable else 0o444)
        except BaseException:
            shutil.rmtree(destination)
            raise
        return destination
