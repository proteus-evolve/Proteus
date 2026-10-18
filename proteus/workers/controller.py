"""Idempotent worker generations and shared budget reservations.

The controller never infers completion from a missing process. A dispatched
generation without a receipt remains uncertain, with its reservation intact.
"""

from pathlib import Path

from proteus.artifacts.store import _mkdir_durable
from proteus.core.contracts import BudgetLimits, PluginRef, require
from proteus.core.ledger import Amounts, DIMENSIONS, Ledger
from proteus.core.serialization import content_hash
from proteus.workers.records import WorkerLaunch, WorkerRef
from proteus.workers.runtime import CommandWorkerRuntime, immutable, lease, write_json


class WorkerController:
    def __init__(self, root, *, identity, options, ledger, scopes, runtime=None, runtime_ref=None, environment=None):
        self.root = Path(root).resolve()
        self.identity, self.options, self.ledger = identity, options, ledger
        Ledger(ledger.path, run_id=identity.run_id, config_hash=identity.config_hash, lock_hash=identity.lock_hash)
        self.scopes = dict(scopes)
        require(self.scopes and all(isinstance(limits, BudgetLimits) for limits in self.scopes.values()),
                "worker requires declared budget scopes")
        self.runtime = runtime or CommandWorkerRuntime()
        self.environment = environment
        self.runtime_ref = runtime_ref or PluginRef(name="proteus.worker.command", version="1")
        self.reservation = self.runtime.reservation(options)
        require(type(self.reservation) is Amounts, "worker runtime must declare typed budget bounds")
        require(all(getattr(limits, "max_" + name) is None or getattr(self.reservation, name) is not None
                    for limits in self.scopes.values() for name in DIMENSIONS),
                "worker runtime cannot enforce finite model-call, cost or wall-time budgets")
        self.private = self.root / "private/workers" / content_hash(identity.worker_id).split(":")[1]
        self.workspace = self.root / "workers" / content_hash(identity.worker_id).split(":")[1] / "workspace"
        _mkdir_durable(self.private)
        _mkdir_durable(self.workspace)
        with lease(self.private / "controller.lock"):
            immutable(self.private / "worker.json", {"identity": identity, "runtime": self.runtime_ref,
                "options": options, "scopes": self.scopes, "reservation": self.reservation})

    def _directory(self, operation_id):
        return self.private / content_hash(operation_id).split(":")[1]

    def _launch(self, ref):
        require(ref.worker_id == self.identity.worker_id, "reference belongs to another worker")
        directory = self._directory(ref.operation_id)
        launch = WorkerLaunch.from_json((directory / "request.json").read_text())
        require(launch.ref() == ref and launch.identity == self.identity and content_hash(launch.options) == content_hash(self.options)
                and launch.runtime == self.runtime_ref
                and launch.workspace == str(self.workspace), "worker launch identity differs")
        return launch, directory

    @staticmethod
    def _entry(ref):
        return "worker:" + ref.request_hash

    def head(self):
        path = self.private / "head.json"
        return WorkerRef.from_json(path.read_text()) if path.exists() else None

    def start(self, operation_id, prompt, *, previous=None, fresh_orientation=None, fallback_reason=None):
        with lease(self.private / "controller.lock"):
            current = self.head()
            existing = (self._directory(operation_id) / "request.json").exists()
            require(existing or current is None or current.operation_id == operation_id or current == previous,
                    "new worker generation must identify the current predecessor")
            session_id, continuity = None, "fresh"
            if previous is not None:
                require(previous.operation_id != operation_id, "worker generation cannot resume itself")
                observation = self.poll(previous)
                require(observation.completion is not None, "previous worker generation has no terminal receipt")
                session_id = observation.completion.session_id
                if fallback_reason is not None:
                    require(fresh_orientation is not None and fresh_orientation.strip(),
                            "fresh fallback requires an orientation prompt")
                    session_id, continuity = None, "fresh_fallback"
                    prompt = fresh_orientation + "\n\n" + prompt
                else:
                    require(session_id is not None, "native session unavailable; explicit fresh fallback required")
                    require(fresh_orientation is None, "fresh orientation requires a fallback reason")
                    continuity = "native"
            else:
                require(fresh_orientation is None and fallback_reason is None, "initial worker has no session to downgrade")
            launch = WorkerLaunch(identity=self.identity, runtime=self.runtime_ref, options=self.options.to_dict(), operation_id=operation_id,
                prompt=prompt, workspace=str(self.workspace), previous=previous, session_id=session_id,
                continuity=continuity, fallback_reason=fallback_reason)
            ref, directory = launch.ref(), self._directory(operation_id)
            _mkdir_durable(directory)
            immutable(directory / "request.json", launch)
            if current is not None and current not in (ref, previous):
                require(self.poll(ref).completion is not None, "superseded worker generation cannot be dispatched")
                return ref
            entry = self.ledger.reserve(self._entry(ref), scopes=self.scopes,
                reservation=self.reservation, metadata={"kind": "worker", "worker_id": self.identity.worker_id,
                    "role": self.identity.role, "operation_id": operation_id}, request=launch, slots=1)
            if current is None or current == previous or current == ref:
                write_json(self.private / "head.json", ref)
            if entry["state"] == "reserved":
                self.ledger.start(self._entry(ref))
            if entry["state"] not in ("completed", "failed", "cancelled"):
                self._start(launch, directory)
            return ref

    def _start(self, launch, directory):
        if self.environment is None:
            self.runtime.start(launch, directory)
        else:
            self.runtime.start(launch, directory, environment=self.environment() if callable(self.environment) else self.environment)

    def ensure_started(self, ref):
        """Reconcile a committed launch after journal replay, without a new request."""
        with lease(self.private / "controller.lock"):
            launch, directory = self._launch(ref)
            entry = self.ledger.entry(self._entry(ref))
            require(entry["state"] != "reserved", "recorded worker launch was not started")
            if entry["state"] == "started":
                require(self.head() == ref, "superseded worker generation cannot be dispatched")
                self._start(launch, directory)

    def poll(self, ref):
        launch, directory = self._launch(ref)
        observed = self.runtime.poll(launch, directory)
        require(observed.ref == ref, "runtime observed a different generation")
        if observed.completion is not None:
            completion = observed.completion
            if launch.continuity == "native" and completion.status in ("completed", "paused"):
                require(completion.resumed_from == launch.session_id,
                        "worker did not confirm continuation of the requested native session")
            violations = self.ledger.settle(self._entry(ref), actual=Amounts.from_usage(completion.usage),
                status="failed" if completion.status == "failed" else "completed", result=completion)
            require(not violations, "worker exceeded its declared reservation")
        return observed

    def control(self, ref, action):
        require(action in ("pause", "stop"), "unknown worker control action")
        launch, directory = self._launch(ref)
        with lease(self.private / "controller.lock"):
            require(self.head() == ref, "control must target the current worker generation")
            observed = self.poll(ref)
            if observed.completion is None:
                self.runtime.control(launch, directory, action)
            return observed
