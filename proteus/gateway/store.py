"""Atomic admission, ordered claims and response receipts outside the method journal."""

from contextlib import contextmanager
import hashlib
import hmac
from pathlib import Path
import secrets
import sqlite3

from proteus.artifacts.store import _mkdir_durable, _sync_directory
from proteus.core.contracts import require
from proteus.core.serialization import canonical_json, content_hash
from proteus.gateway.records import GatewayClaim, GatewayPoll, GatewayResult, GatewayTicket
from proteus.workers.runtime import immutable


class GatewayStore:
    def __init__(self, root, *, identity, members, task_counts, pending_limit, evaluation_limit):
        self.root = Path(root).resolve()
        _mkdir_durable(self.root)
        self.path = self.root / "queue.sqlite3"
        require(not self.path.is_symlink(), "gateway queue cannot be a symlink")
        marker = self.root / "store.json"
        require(self.path.exists() or not marker.exists(), "gateway queue is missing; refusing to replace its credentials or admissions")
        self.identity, self.members, self.task_counts = identity, tuple(members), dict(task_counts)
        self.pending_limit, self.evaluation_limit = pending_limit, evaluation_limit
        self.policy = {"identity": identity, "members": self.members, "task_counts": self.task_counts,
                       "pending_limit": pending_limit, "evaluation_limit": evaluation_limit}
        with self.transaction() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            require(version in (0, 1), "unsupported gateway schema")
            if version == 0:
                require(not tables and not marker.exists(), "refusing to initialize an unknown gateway database")
                db.execute("CREATE TABLE identity (policy TEXT NOT NULL)")
                db.execute("CREATE TABLE requests (sequence INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL, "
                           "worker TEXT NOT NULL, operation TEXT NOT NULL, ticket TEXT NOT NULL, "
                           "status TEXT NOT NULL, result TEXT, UNIQUE(worker,operation))")
                db.execute("CREATE TABLE claims (operation TEXT PRIMARY KEY, ticket TEXT)")
                db.execute("CREATE TABLE credentials (worker TEXT PRIMARY KEY, token TEXT NOT NULL UNIQUE)")
                db.executemany("INSERT INTO credentials VALUES (?,?)",
                               ((worker, secrets.token_urlsafe(32)) for worker in self.members))
                db.execute("INSERT INTO identity VALUES (?)", (canonical_json(self.policy),))
                db.execute("PRAGMA user_version=1")
            else:
                require(tables == {"identity", "requests", "claims", "credentials"}, "gateway schema differs")
                rows = db.execute("SELECT policy FROM identity").fetchall()
                require(len(rows) == 1 and rows[0][0] == canonical_json(self.policy), "gateway identity or policy changed")
            tokens = dict(db.execute("SELECT worker,token FROM credentials"))
            require(set(tokens) == set(self.members) and all(type(token) is str and len(token) >= 32
                    for token in tokens.values()) and len(set(tokens.values())) == len(tokens),
                    "gateway credentials are invalid or duplicated")
            self._tokens = tokens
        self.path.chmod(0o600)
        _sync_directory(self.root)
        immutable(marker, {"schema_version": 1, "policy_hash": content_hash(self.policy)})

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    def credential(self, worker):
        require(worker in self.members, "worker is not a gateway member")
        return self._tokens[worker]

    def authenticate(self, token):
        digest = hashlib.sha256(token.encode()).digest()
        matches = [worker for worker, expected in self._tokens.items()
                   if hmac.compare_digest(digest, hashlib.sha256(expected.encode()).digest())]
        require(len(matches) == 1, "invalid gateway credential")
        return matches[0]

    def _ticket(self, row):
        require(row is not None, "unknown gateway submission")
        ticket = GatewayTicket.from_json(row["ticket"])
        require(ticket.identity_hash == content_hash(self.identity) and ticket.id() == row["id"]
                and (ticket.worker_id, ticket.operation_id) == (row["worker"], row["operation"]),
                "gateway ticket integrity mismatch")
        return ticket

    def admit(self, worker, submission, artifact):
        require(worker in self.members and submission.evaluation_id in self.task_counts,
                "worker or evaluation is outside the gateway policy")
        ticket = GatewayTicket(identity_hash=content_hash(self.identity), worker_id=worker,
            operation_id=submission.operation_id, request_hash=content_hash(submission), artifact=artifact,
            evaluation_id=submission.evaluation_id, task_count=self.task_counts[submission.evaluation_id])
        with self.transaction() as db:
            previous = self._capacity(db, worker, submission)
            if previous is not None:
                require(previous == ticket, "gateway operation was reused with different inputs")
                return ticket
            db.execute("INSERT INTO requests(id,worker,operation,ticket,status) VALUES (?,?,?,?,'queued')",
                       (ticket.id(), worker, ticket.operation_id, ticket.to_json()))
        return ticket

    def _capacity(self, db, worker, submission):
        require(worker in self.members and submission.evaluation_id in self.task_counts,
                "worker or evaluation is outside the gateway policy")
        previous = db.execute("SELECT * FROM requests WHERE worker=? AND operation=?",
                              (worker, submission.operation_id)).fetchone()
        if previous is not None:
            ticket = self._ticket(previous)
            require(ticket.request_hash == content_hash(submission), "gateway operation was reused with different inputs")
            return ticket
        rows = db.execute("SELECT * FROM requests WHERE worker=?", (worker,)).fetchall()
        pending = sum(row["status"] in ("queued", "processing") for row in rows)
        require(pending < self.pending_limit, "worker gateway queue is full")
        admitted = sum(self._ticket(row).task_count for row in rows)
        require(self.evaluation_limit is None
                or admitted + self.task_counts[submission.evaluation_id] <= self.evaluation_limit,
                "worker gateway evaluation admission budget exhausted")
        return None

    def preflight(self, worker, submission):
        # Avoid writing arbitrary unused artifact blobs after a worker's queue
        # or admission budget is full. admit() rechecks atomically after upload.
        with self.transaction() as db:
            return self._capacity(db, worker, submission)

    def claim(self, operation_id):
        require(type(operation_id) is str and operation_id.strip(), "gateway claim needs an operation ID")
        with self.transaction() as db:
            prior = db.execute("SELECT ticket FROM claims WHERE operation=?", (operation_id,)).fetchone()
            if prior is not None:
                ticket = GatewayTicket.from_json(prior[0]) if prior[0] is not None else None
                if ticket is not None:
                    require(self._ticket(db.execute("SELECT * FROM requests WHERE id=?", (ticket.id(),)).fetchone()) == ticket,
                            "gateway claim no longer matches its request")
                return GatewayClaim(ticket=ticket)
            row = db.execute("SELECT * FROM requests WHERE status='queued' ORDER BY sequence LIMIT 1").fetchone()
            ticket = self._ticket(row) if row is not None else None
            if ticket is not None:
                db.execute("UPDATE requests SET status='processing' WHERE id=?", (ticket.id(),))
            db.execute("INSERT INTO claims VALUES (?,?)", (operation_id, ticket.to_json() if ticket is not None else None))
        return GatewayClaim(ticket=ticket)

    def claim_ready(self, operation_id):
        with self.transaction() as db:
            return (db.execute("SELECT 1 FROM claims WHERE operation=?", (operation_id,)).fetchone() is not None
                    or db.execute("SELECT 1 FROM requests WHERE status='queued' LIMIT 1").fetchone() is not None)

    def poll(self, worker, ticket_id):
        with self.transaction() as db:
            row = db.execute("SELECT * FROM requests WHERE id=? AND worker=?", (ticket_id, worker)).fetchone()
            ticket = self._ticket(row)
            result = GatewayResult.from_json(row["result"]) if row["result"] is not None else None
            return GatewayPoll(ticket=ticket, status=row["status"], result=result)

    def finish(self, result):
        ticket = result.ticket
        with self.transaction() as db:
            row = db.execute("SELECT * FROM requests WHERE id=?", (ticket.id(),)).fetchone()
            require(self._ticket(row) == ticket, "gateway result belongs to another request")
            if row["result"] is not None:
                require(GatewayResult.from_json(row["result"]) == result, "gateway response changed after publication")
                return result
            require(row["status"] == "processing", "gateway evaluation has not been claimed")
            db.execute("UPDATE requests SET status=?, result=? WHERE id=?", (result.status, result.to_json(), ticket.id()))
        return result

    def pending(self):
        with self.transaction() as db:
            return tuple(self._ticket(row) for row in db.execute(
                "SELECT * FROM requests WHERE status IN ('queued','processing') ORDER BY sequence"))

    def has_requests(self):
        with self.transaction() as db:
            return db.execute("SELECT 1 FROM requests LIMIT 1").fetchone() is not None
