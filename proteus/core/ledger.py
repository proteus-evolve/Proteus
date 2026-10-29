"""Durable dispatch reservations and usage, with atomic multi-scope accounting.

This is a local SQLite ledger, not an external exactly-once executor. A started
entry without a settlement is uncertain after a crash and retains its reservation
and concurrency slot. Reconciliation/replay is an explicit orchestration concern.
Payloads may contain private evidence; keep this database under the run's private/
directory and export only deliberately selected records.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from proteus.artifacts.store import _mkdir_durable, _sync_directory
from proteus.core.contracts import NONNEGATIVE, BudgetLimits, Usage, require
from proteus.core.serialization import Contract, canonical_json, read_json


DIMENSIONS = ("model_calls", "evaluations", "cost_usd", "wall_seconds")
TERMINAL = ("completed", "failed", "cancelled")


@dataclass(frozen=True, kw_only=True)
class Amounts(Contract):
    model_calls: int | None = field(default=None, metadata=NONNEGATIVE)
    evaluations: int | None = field(default=0, metadata=NONNEGATIVE)
    cost_usd: float | None = field(default=None, metadata=NONNEGATIVE)
    wall_seconds: float | None = field(default=None, metadata=NONNEGATIVE)

    @classmethod
    def from_usage(cls, usage: Usage):
        return cls(model_calls=usage.model_calls, cost_usd=usage.cost_usd,
                   wall_seconds=usage.wall_seconds)

    @classmethod
    def zero(cls, *, evaluations=0):
        return cls(model_calls=0, evaluations=evaluations, cost_usd=0, wall_seconds=0)


class Ledger:
    """Every write uses a fresh connection and BEGIN IMMEDIATE on a local filesystem.

    Limits cannot be increased by reopening a run or re-registering a scope.
    Reservation IDs are idempotent identities, not permission to repeat work.
    Decimal arithmetic compares resource totals without binary-float cap errors.
    """

    def __init__(self, path: Path, *, run_id: str, config_hash: str, lock_hash: str | None):
        require(bool(run_id) and bool(config_hash), "ledger requires run and configuration identity")
        self.path = Path(path)
        _mkdir_durable(self.path.parent)
        require(not self.path.is_symlink(), "ledger cannot be a symlink")
        identity = canonical_json({"schema_version": 1, "run_id": run_id,
                                   "config_hash": config_hash, "lock_hash": lock_hash})
        with self._transaction() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            require(version in (0, 1, 2), "unsupported ledger schema version")
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if version == 0:
                require(not tables, "refusing to initialize an unrecognized ledger database")
                for sql in (
                    "CREATE TABLE identity (value TEXT NOT NULL)",
                    "CREATE TABLE scopes (id TEXT PRIMARY KEY, limits_json TEXT NOT NULL)",
                    "CREATE TABLE entries (id TEXT PRIMARY KEY, identity_json TEXT NOT NULL, "
                    "state TEXT NOT NULL, settlement_json TEXT)",
                    "CREATE TABLE membership (scope TEXT NOT NULL REFERENCES scopes(id), "
                    "entry TEXT NOT NULL REFERENCES entries(id), PRIMARY KEY(scope, entry))",
                    "CREATE TABLE events (sequence INTEGER PRIMARY KEY, entry TEXT NOT NULL "
                    "REFERENCES entries(id), kind TEXT NOT NULL, time_ns INTEGER NOT NULL, payload_json TEXT NOT NULL)",
                ):
                    db.execute(sql)
                db.execute("INSERT INTO identity VALUES (?)", (identity,))
                db.execute("PRAGMA user_version=1")
            else:
                require(tables == ({"identity", "scopes", "entries", "membership", "events"}
                                   | ({"balances"} if version == 2 else set())),
                        "ledger schema is incomplete or unrecognized")
                identities = db.execute("SELECT value FROM identity").fetchall()
                require(len(identities) == 1 and identities[0][0] == identity,
                        "ledger run/configuration/lock identity differs")
            if version != 2:
                db.execute("ALTER TABLE entries ADD COLUMN operation_id TEXT")
                db.execute("CREATE INDEX entries_operation ON entries(operation_id)")
                db.execute("CREATE TABLE balances (scope TEXT PRIMARY KEY REFERENCES scopes(id), totals_json TEXT NOT NULL)")
                totals = {row[0]: self._empty_totals() for row in db.execute("SELECT id FROM scopes")}
                for row in db.execute("SELECT * FROM entries").fetchall():
                    entry = self._row(row)
                    db.execute("UPDATE entries SET operation_id=? WHERE id=?",
                               (entry['identity']['metadata'].get('operation_id'), entry['id']))
                    for scope in entry['identity']['scopes']:
                        self._apply(totals[scope], entry, 1)
                for scope, value in totals.items():
                    db.execute("INSERT INTO balances VALUES (?,?)", (scope, canonical_json(value)))
                db.execute("PRAGMA user_version=2")
        _sync_directory(self.path.parent)

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=FULL")
            yield db
        finally:
            db.close()

    @contextmanager
    def _transaction(self):
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.execute("COMMIT")
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise

    @staticmethod
    def _row(row):
        require(row is not None, "unknown ledger entry")
        return {"id": row["id"], "identity": read_json(row["identity_json"]),
                "state": row["state"], "settlement": read_json(row["settlement_json"])
                if row["settlement_json"] is not None else None}

    @classmethod
    def _entry(cls, db, entry_id):
        return cls._row(db.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone())

    @staticmethod
    def _event(db, entry_id, kind, payload):
        db.execute("INSERT INTO events(entry, kind, time_ns, payload_json) VALUES (?,?,?,?)",
                   (entry_id, kind, time.time_ns(), canonical_json(payload)))

    @staticmethod
    def _scan_balance(db, scope):
        rows = db.execute("SELECT e.* FROM entries e JOIN membership m ON e.id=m.entry "
                          "WHERE m.scope=? ORDER BY e.id", (scope,)).fetchall()
        charged = {key: Decimal(0) for key in DIMENSIONS}
        unknown = {key: 0 for key in DIMENSIONS}
        active_slots = 0
        for row in rows:
            entry = Ledger._row(row)
            reserved = entry["identity"]["reservation"]
            settlement = entry["settlement"]
            if entry["state"] not in TERMINAL:
                active_slots += entry["identity"]["slots"]
            for key in DIMENSIONS:
                actual = settlement["actual"][key] if settlement is not None else None
                if actual is None:
                    unknown[key] += 1
                value = actual if actual is not None else reserved[key]
                if value is None or charged[key] is None:
                    charged[key] = None
                else:
                    charged[key] += Decimal(str(value))
        return charged, unknown, active_slots, len(rows)

    @staticmethod
    def _empty_totals():
        return {"sum": {key: "0" for key in DIMENSIONS},
                "unknown": {key: 0 for key in DIMENSIONS},
                "unbounded": {key: 0 for key in DIMENSIONS}, "active_slots": 0, "entries": 0}

    @staticmethod
    def _apply(totals, entry, sign):
        totals["entries"] += sign
        if entry["state"] not in TERMINAL:
            totals["active_slots"] += sign * entry["identity"]["slots"]
        for key in DIMENSIONS:
            actual = entry["settlement"]["actual"][key] if entry["settlement"] is not None else None
            effective = actual if actual is not None else entry["identity"]["reservation"][key]
            totals["unknown"][key] += sign * int(actual is None)
            totals["unbounded"][key] += sign * int(effective is None)
            totals["sum"][key] = str(Decimal(totals["sum"][key]) + sign * Decimal(str(effective or 0)))

    @classmethod
    def _adjust(cls, db, entry, sign):
        for scope in entry["identity"]["scopes"]:
            row = db.execute("SELECT totals_json FROM balances WHERE scope=?", (scope,)).fetchone()
            require(row is not None, "ledger scope balance is missing")
            totals = read_json(row[0])
            cls._apply(totals, entry, sign)
            db.execute("UPDATE balances SET totals_json=? WHERE scope=?", (canonical_json(totals), scope))

    @staticmethod
    def _balance(db, scope):
        row = db.execute("SELECT totals_json FROM balances WHERE scope=?", (scope,)).fetchone()
        totals = read_json(row[0]) if row is not None else Ledger._empty_totals()
        charged = {key: None if totals["unbounded"][key] else Decimal(totals["sum"][key])
                   for key in DIMENSIONS}
        return charged, totals["unknown"], totals["active_slots"], totals["entries"]

    def reserve(self, entry_id: str, *, scopes: Mapping[str, BudgetLimits], reservation: Amounts,
                metadata: Mapping, request, slots: int = 1, reuse: bool = True):
        require(type(entry_id) is str and bool(entry_id.strip()), "ledger entry id must be nonempty")
        require(type(slots) is int and slots >= 0, "invalid concurrency reservation")
        require(isinstance(metadata, Mapping), "ledger metadata must be a JSON object")
        require(bool(scopes) and all(type(key) is str and key.strip() for key in scopes),
                "ledger reservations require named scopes")
        require(type(reservation) is Amounts and all(type(v) is BudgetLimits for v in scopes.values()),
                "ledger requires typed reservations and limits")
        identity = {"scopes": sorted(scopes), "reservation": reservation, "slots": slots,
                    "metadata": metadata, "request": request}
        encoded = canonical_json(identity)
        with self._transaction() as db:
            for scope, limits in scopes.items():
                encoded_limits = canonical_json(limits)
                old = db.execute("SELECT limits_json FROM scopes WHERE id=?", (scope,)).fetchone()
                require(old is None or old[0] == encoded_limits, f"budget scope changed: {scope}")
                if old is None:
                    db.execute("INSERT INTO scopes VALUES (?,?)", (scope, encoded_limits))
                    db.execute("INSERT INTO balances VALUES (?,?)", (scope, canonical_json(self._empty_totals())))
            old = db.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
            if old is not None:
                require(old["identity_json"] == encoded, "ledger operation identity changed")
                require(reuse, "ledger entry already submitted; recovery requires explicit reconciliation")
                return self._row(old)
            for scope, limits in scopes.items():
                charged, _, active, _ = self._balance(db, scope)
                require(active + slots <= limits.max_concurrency, f"concurrency budget exhausted: {scope}")
                for key in DIMENSIONS:
                    cap, bound = getattr(limits, "max_" + key), getattr(reservation, key)
                    if cap is not None:
                        require(bound is not None, f"runtime cannot enforce a finite {key} reservation")
                        require(charged[key] is not None
                                and charged[key] + Decimal(str(bound)) <= Decimal(str(cap)),
                                f"{'evaluation' if key == 'evaluations' else key} budget exhausted: {scope}")
            db.execute("INSERT INTO entries(id,identity_json,state,settlement_json,operation_id) VALUES (?,?,'reserved',NULL,?)",
                       (entry_id, encoded, metadata.get("operation_id")))
            for scope in scopes:
                db.execute("INSERT INTO membership VALUES (?,?)", (scope, entry_id))
            self._adjust(db, self._entry(db, entry_id), 1)
            self._event(db, entry_id, "reserved", identity)
            return self._entry(db, entry_id)

    def start(self, entry_id):
        with self._transaction() as db:
            entry = self._entry(db, entry_id)
            require(entry["state"] == "reserved", "ledger entry already started or settled; reconcile explicitly")
            db.execute("UPDATE entries SET state='started' WHERE id=?", (entry_id,))
            self._event(db, entry_id, "started", {})

    def settle(self, entry_id, *, actual: Amounts, status: str, result=None):
        """Commit actual usage even if it exceeds the bound; return violations.

        Callers must fail/report the violation after this transaction commits. A
        terminal retry is accepted only if its entire settlement is identical.
        Unknown actual usage retains the corresponding reservation in accounting.
        """
        require(type(actual) is Amounts and status in TERMINAL, "invalid ledger settlement")
        with self._transaction() as db:
            entry = self._entry(db, entry_id)
            if status == "cancelled":
                require(entry["state"] == "reserved" or entry["state"] == "cancelled",
                        "started work cannot be cancelled without accounting for its effects")
                require(actual == Amounts.zero(), "pre-dispatch cancellation must have zero usage")
            else:
                require(entry["state"] != "reserved", "settlement requires started work")
            violations = []
            for key in DIMENSIONS:
                bound, value = entry["identity"]["reservation"][key], getattr(actual, key)
                if bound is not None and value is not None and Decimal(str(value)) > Decimal(str(bound)):
                    violations.append(key)
            settlement = {"actual": actual, "status": status, "result": result, "violations": violations}
            encoded = canonical_json(settlement)
            if entry["state"] in TERMINAL:
                require(canonical_json(entry["settlement"]) == encoded, "ledger settlement changed")
                return tuple(violations)
            self._adjust(db, entry, -1)
            db.execute("UPDATE entries SET state=?, settlement_json=? WHERE id=?",
                       (status, encoded, entry_id))
            self._adjust(db, self._entry(db, entry_id), 1)
            self._event(db, entry_id, "settled", settlement)
            return tuple(violations)

    def entry(self, entry_id):
        with self._connection() as db:
            return self._entry(db, entry_id)

    def entries(self):
        with self._connection() as db:
            return [self._row(row) for row in db.execute("SELECT * FROM entries ORDER BY id")]

    def has_operation(self, operation_id):
        with self._connection() as db:
            return db.execute("SELECT 1 FROM entries WHERE operation_id=? LIMIT 1", (operation_id,)).fetchone() is not None

    def balance(self, scope="run"):
        with self._connection() as db:
            charged, unknown, slots, count = self._balance(db, scope)
        return {"charged": {k: None if v is None else int(v) if k in ("model_calls", "evaluations")
                            else float(v) for k, v in charged.items()},
                "unknown": unknown, "active_slots": slots, "entries": count}

    def events(self, *, after=0):
        with self._connection() as db:
            return [{"sequence": row["sequence"], "entry": row["entry"], "kind": row["kind"],
                     "time_ns": row["time_ns"], "payload": read_json(row["payload_json"])}
                    for row in db.execute("SELECT * FROM events WHERE sequence>? ORDER BY sequence", (after,))]
