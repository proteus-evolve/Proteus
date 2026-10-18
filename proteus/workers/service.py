"""Declared worker runtimes exposed to method orchestration."""

from proteus.core.contracts import require
from proteus.core.ledger import Amounts, DIMENSIONS
from proteus.workers.controller import WorkerController
from proteus.workers.records import WorkerIdentity, WorkerObservation, WorkerRef


def scopes(recipe, spec):
    return {"run": recipe.budget, "role:" + spec.role: recipe.actors[spec.role].budget,
            "worker:" + spec.id: spec.budget}


def validate_workers(recipe, registry):
    for spec in recipe.workers:
        plugin = registry.get(spec.runtime.plugin, "worker")
        require({"worker.lifecycle", "worker.reconcile"} <= set(plugin.capabilities),
                "worker runtime must support lifecycle and reconciliation")
        options = plugin.options.from_dict(spec.runtime.options)
        runtime = registry.create(spec.runtime, "worker")
        bounds = runtime.reservation(options)
        require(type(bounds) is Amounts, "worker runtime must declare typed budget bounds")
        require(all(getattr(limits, "max_" + name) is None or getattr(bounds, name) is not None
                    for limits in scopes(recipe, spec).values() for name in DIMENSIONS),
                "worker runtime cannot enforce the configured finite budgets")
        if plugin.validate is not None:
            plugin.validate(recipe, options)


class WorkerService:
    def __init__(self, services, worker_id):
        self.services = services
        self.spec = next((item for item in services.recipe.workers if item.id == worker_id), None)
        require(self.spec is not None, "unknown declared worker")
        self._controller = None

    @property
    def controller(self):
        if self._controller is None:
            services, spec = self.services, self.spec
            plugin = services.registry.get(spec.runtime.plugin, "worker")
            self._controller = WorkerController(services.root,
                identity=WorkerIdentity(run_id=services.run_id, config_hash=services.recipe.config_hash,
                    lock_hash=services.lock_hash, worker_id=spec.id, role=spec.role),
                options=plugin.options.from_dict(spec.runtime.options), ledger=services.ledger,
                scopes=scopes(services.recipe, spec), runtime=services.registry.create(spec.runtime, "worker"),
                runtime_ref=spec.runtime.plugin)
        return self._controller

    def start(self, operation_id, prompt, *, previous=None, fresh_orientation=None, fallback_reason=None):
        ref = self.services._record("worker.start", {"worker_id": self.spec.id, "operation_id": operation_id,
            "prompt": prompt, "previous": previous, "fresh_orientation": fresh_orientation,
            "fallback_reason": fallback_reason},
            lambda: self.controller.start(operation_id, prompt, previous=previous,
                fresh_orientation=fresh_orientation, fallback_reason=fallback_reason), decode=WorkerRef.from_dict)
        self.controller.ensure_started(ref)
        self.services.values.publish("proteus.worker.ref@1", ref)
        return ref

    def poll(self, ref):
        # Dynamic observations must not be replayed as permanently "active".
        # Launch/control identities and terminal receipts are durable separately.
        return self.controller.poll(ref)

    def control(self, ref, action):
        return self.services._record("worker.control", {"worker_id": self.spec.id, "ref": ref, "action": action},
            lambda: self.controller.control(ref, action), decode=WorkerObservation.from_dict)

    def result(self, ref):
        result = self.poll(ref)
        require(result.completion is not None, "worker has no terminal completion receipt")
        return self.services.values.publish("proteus.worker.completion@1", result.completion)


def validate_restored(services):
    references = {}
    for ref, value in services.values._values.values():
        if ref.kind == "proteus.worker.ref@1":
            worker = WorkerService(services, value.worker_id).controller
            worker._launch(value)
            references[value.request_hash] = (worker, value)
    for ref, value in services.values._values.values():
        if ref.kind == "proteus.worker.completion@1":
            require(value.request_hash in references, "restored worker result has no launch reference")
            worker, reference = references[value.request_hash]
            require(worker.poll(reference).completion == value, "restored worker completion differs from its receipt")


def require_terminal(services):
    from proteus.core.recovery import RecoveryRequired
    for spec in services.recipe.workers:
        worker = WorkerService(services, spec.id).controller
        head = worker.head()
        if head is not None:
            if worker.poll(head).completion is None:
                raise RecoveryRequired("method cannot complete with an active or uncertain worker")
