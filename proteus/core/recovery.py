"""Durable serial operation replay and explicit method checkpoints.

Immutable request/result pairs are published under the run's single-writer lock.
Only a committed result permits skipping an operation. A dispatched call without
a settled result is uncertain, never permission to execute it again.
"""

from __future__ import annotations

from dataclasses import replace
import threading

from proteus.artifacts.store import _read, publish_bytes
from proteus.core.contracts import MethodState, PluginState, require
from proteus.core.serialization import ContractError, canonical_json, content_hash, read_json


class RecoveryRequired(RuntimeError):
    """Execution must stop until an uncertain external operation is reconciled."""


def _write(path, value):
    publish_bytes(path, canonical_json({"hash": content_hash(value), "value": value}).encode())


def _load(path):
    envelope = read_json(_read(path).decode())
    require(set(envelope) == {"hash", "value"} and content_hash(envelope["value"]) == envelope["hash"],
            "recovery record integrity mismatch")
    return envelope["value"]


def tuples(value):
    return tuple(tuples(item) for item in value) if isinstance(value, list) else value


def recovery_status(root):
    """Read recovery metadata without exposing prompts, task references or grades."""
    from pathlib import Path
    from proteus.core.ledger import Ledger
    from proteus.recipes.manifest import RunManifest
    root = Path(root)
    manifest = RunManifest.from_json((root / "manifest.json").read_text())
    directory = root / "private/recovery"
    require(_load(directory / "origin.json")["identity"] == {"manifest_hash": manifest.manifest_hash, "format": 1},
            "recovery journal belongs to another manifest")
    checkpoint, pending, terminal = None, None, False
    for sequence, path in enumerate(sorted((directory / "operations").glob("*.request.json")), 1):
        require(path.name == f"{sequence:08d}.request.json", "recovery operation sequence has a gap")
        request = _load(path)
        result_path = path.with_name(f"{sequence:08d}.result.json")
        if not result_path.exists():
            require(pending is None, "multiple incomplete serial operations")
            pending = {"sequence": sequence, "kind": request["kind"], "request_hash": content_hash(request)}
            continue
        require(pending is None, "uncommitted operation precedes later operations")
        result = _load(result_path)
        require(result["request_hash"] == content_hash(request), "recovery result/request mismatch")
        if request["kind"] == "checkpoint":
            checkpoint = {"cursor": sequence, "sampling_cursor": result["output"]["sampling_cursor"],
                          "pending_operations": [item["operation_id"] for item in result["output"]["pending_operations"]]}
        terminal = request["kind"] == "method.result"
    unsettled = []
    ledger_path = root / "private/ledger.sqlite3"
    if ledger_path.exists():
        ledger = Ledger(ledger_path, run_id=manifest.run_id, config_hash=manifest.recipe.config_hash, lock_hash=manifest.lock_hash)
        for entry in ledger.entries():
            if entry["state"] in ("reserved", "started"):
                metadata = entry["identity"]["metadata"]
                unsettled.append({"id": entry["id"], "state": entry["state"],
                    **{key: metadata.get(key) for key in ("kind", "operation_id", "attempt_id", "trial_id")}})
    return {"run_id": manifest.run_id, "checkpoint": checkpoint, "pending": pending,
            "terminal_result": terminal, "unsettled": unsettled}


class Journal:
    def __init__(self, services, *, manifest_hash, validate_state):
        self.services = services
        self.root = services.root / "private/recovery"
        self.cursor = 0
        self.active = False
        self.recovering = False
        self.validate_state = validate_state
        self.identity = {"manifest_hash": manifest_hash, "format": 1}

    def _path(self, sequence, suffix):
        return self.root / "operations" / f"{sequence:08d}.{suffix}.json"

    @property
    def pending(self):
        return self._path(self.cursor + 1, "request").exists() and not self._path(self.cursor + 1, "result").exists()

    def initialize(self):
        origin = self.root / "origin.json"
        if not self.services.resume:
            _write(origin, {"identity": self.identity, "snapshot": self.services.snapshot()})
            return None, None
        require(origin.is_file(), "run has no durable recovery journal")
        record = _load(origin)
        require(record["identity"] == self.identity, "recovery journal belongs to another manifest")
        snapshot, state, final = record["snapshot"], None, None
        paths = sorted((self.root / "operations").glob("*.request.json"))
        requests = {path.name.removesuffix(".request.json") for path in paths}
        require(all(path.name.removesuffix(".result.json") in requests
                    for path in (self.root / "operations").glob("*.result.json")), "orphan recovery result")
        for sequence, path in enumerate(paths, 1):
            require(path == self._path(sequence, "request"), "recovery operation sequence has a gap")
            request = _load(path)
            result_path = self._path(sequence, "result")
            if not result_path.exists():
                require(sequence == len(paths), "uncommitted operation precedes later operations")
                break
            result = _load(result_path)
            require(result["request_hash"] == content_hash(request), "recovery result/request mismatch")
            if request["kind"] == "checkpoint":
                require(result["error"] is None, "invalid checkpoint result")
                state = MethodState.from_dict(result["output"])
                self.validate_state(state)
                require(state.checkpoint_cursor == sequence, "checkpoint cursor differs from journal")
                snapshot, self.cursor = result["snapshot"], sequence
            if request["kind"] == "method.result":
                require(sequence == len(paths) and result["error"] is None, "method result must be terminal")
                snapshot, self.cursor, final = result["snapshot"], sequence, result["output"]
        self.services.restore(snapshot)
        return state, final

    def call(self, kind, identity, function, *, encode=lambda value: value.to_dict(), decode=None):
        require(threading.get_ident() == self.services._dispatch_thread, "journal requires serial service dispatch")
        require(not self.active, "journal operations cannot be nested")
        sequence = self.cursor + 1
        request = {"kind": kind, "identity": identity}
        request_path, result_path = self._path(sequence, "request"), self._path(sequence, "result")
        recovering = request_path.exists()
        if recovering:
            require(_load(request_path) == read_json(canonical_json(request)),
                    "recovery control flow or operation inputs changed")
            if result_path.exists():
                result = _load(result_path)
                require(result["request_hash"] == content_hash(request), "recovery result/request mismatch")
                self.services.restore(result["snapshot"])
                self.cursor = sequence
                self._raise(result["error"])
                return decode(result["output"]) if decode else result["output"]
        else:
            _write(request_path, request)
        self.active, self.recovering = True, recovering
        try:
            output = function()
        except RecoveryRequired:
            raise
        except Exception as exc:
            # A known failure is also an outcome. Replaying it must not reissue
            # its external calls. Uncertain dispatches bypass this commit.
            error = {"type": type(exc).__name__, "message": str(exc)}
            if hasattr(exc, "result"):
                error["result"] = exc.result.to_dict()
            _write(result_path, {"request_hash": content_hash(request), "output": None,
                                "error": error, "snapshot": self.services.snapshot()})
            self.cursor = sequence
            raise
        else:
            _write(result_path, {"request_hash": content_hash(request), "output": encode(output),
                                "error": None, "snapshot": self.services.snapshot()})
            self.cursor = sequence
            return output
        finally:
            self.active = self.recovering = False

    @staticmethod
    def _raise(error):
        if error is None:
            return
        if error["type"] == "StageFailed":
            from proteus.core.contracts import StageResult
            from proteus.core.methods import StageFailed
            raise StageFailed(StageResult.from_dict(error["result"]))
        kinds = {"ContractError": ContractError, "ValueError": ValueError, "TypeError": TypeError,
                 "OSError": OSError, "RuntimeError": RuntimeError}
        raise kinds.get(error["type"], RuntimeError)(error["message"])

    def checkpoint(self, payload, *, archive=None, sampling_cursor=0, random_state=None):
        services = self.services
        definition = services.registry.get(services.recipe.method.plugin)
        require(type(payload) is definition.state_schema, "checkpoint payload has the wrong method schema")
        if archive is not None:
            services.values.get(archive)
        state = MethodState(run_id=services.run_id, method=services.recipe.method.plugin,
            config_hash=services.recipe.config_hash,
            state=PluginState(namespace=services.recipe.method.plugin.name,
                              payload_version=definition.state_version, payload=payload.to_dict()),
            archive=archive, sampling_cursor=sampling_cursor,
            random_state={"framework": services.rng.getstate(), "method": random_state or {}},
            pending_operations=tuple(session.request for session in services._evaluation_service.sessions
                                     if not session.closed))
        self.validate_state(state)
        return self.call("checkpoint", state, lambda: replace(state, checkpoint_cursor=self.cursor + 1),
                         decode=MethodState.from_dict)

    def finish(self, result):
        from proteus.core.methods import MethodResult
        return self.call("method.result", {}, lambda: result, decode=MethodResult.from_dict)
