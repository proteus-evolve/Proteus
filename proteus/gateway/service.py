"""Worker admission and a method-thread pump over existing formal evaluation."""

from proteus.artifacts.codecs import FileWrite
from proteus.core.contracts import require
from proteus.core.recovery import RecoveryRequired
from proteus.core.serialization import content_hash, read_json
from proteus.evidence.store import EvidenceStore
from proteus.gateway.records import GatewayClaim, GatewayIdentity, GatewayResult
from proteus.gateway.store import GatewayStore


class GatewayService:
    def __init__(self, services, gateway_id):
        self.services = services
        self.spec = next((item for item in services.recipe.gateways if item.id == gateway_id), None)
        require(self.spec is not None, "unknown declared evaluation gateway")
        self.identity = GatewayIdentity(run_id=services.run_id, config_hash=services.recipe.config_hash,
            lock_hash=services.lock_hash, gateway_id=gateway_id)
        self.root = services.root / "private/gateways" / content_hash(gateway_id).split(":")[1]
        self._store = None

    @property
    def store(self):
        if self._store is None:
            counts = {spec.id: len(spec.tasks.task_ids) for spec in self.services.recipe.evaluations
                      if spec.id in self.spec.evaluations}
            self._store = GatewayStore(self.root, identity=self.identity, members=self.spec.members,
                task_counts=counts, pending_limit=self.spec.max_pending_per_worker,
                evaluation_limit=self.spec.max_evaluations_per_worker)
        return self._store

    def submit(self, worker_id, submission):
        # Called by the authenticated transport; never accepts a claimed score,
        # worker identity from a request body, private DataRef or task override.
        require(worker_id in self.spec.members and submission.evaluation_id in self.spec.evaluations,
                "worker or evaluation is outside the gateway policy")
        require(len(submission.files) <= self.spec.max_files
                and len(submission.to_json().encode()) <= self.spec.max_request_bytes,
                "gateway submission exceeds configured size limits")
        previous = self.store.preflight(worker_id, submission)
        if previous is not None:
            self.services.artifacts.validate(previous.artifact)
            return previous
        files = {item.path: item.content() for item in submission.files}
        initial = self.services.recipe.subject.initial_artifact
        codec = self.services.artifacts._codec(initial.codec)
        artifact = self.services.artifacts.put(initial.codec, codec.decode(files), runtime=initial.runtime,
                                               environment_hash=initial.environment_hash)
        require(self.services.artifacts.files(artifact) == files,
                "uploaded files are not the codec's exact canonical representation")
        return self.store.admit(worker_id, submission, artifact)

    def claim(self, operation_id, *, timeout_seconds=0):
        import math
        import time
        require(type(timeout_seconds) in (int, float) and math.isfinite(timeout_seconds) and timeout_seconds >= 0,
                "gateway claim timeout must be nonnegative and finite")
        def capture():
            deadline = time.monotonic() + timeout_seconds
            while not self.store.claim_ready(operation_id) and time.monotonic() < deadline:
                time.sleep(min(0.025, max(0, deadline - time.monotonic())))
            result = self.store.claim(operation_id)
            if result.ticket is not None:
                self.services.values.publish("proteus.gateway.ticket@1", result.ticket)
            return result
        return self.services._record("gateway.claim", {"gateway_id": self.spec.id, "operation_id": operation_id,
            "timeout_seconds": timeout_seconds},
            capture, decode=GatewayClaim.from_dict).ticket

    def process(self, ticket, *, inputs=None):
        accepted = self.store.poll(ticket.worker_id, ticket.id())
        require(accepted.ticket == ticket,
                "gateway ticket differs from its accepted request")
        require(accepted.status != "queued", "gateway request must be claimed before evaluation")
        services, candidate = self.services, None
        operation = "gateway:" + ticket.id()
        previous_worker = getattr(services, "_worker_id", None)
        services._worker_id = ticket.worker_id
        try:
            candidate = services.create_candidate(ticket.artifact, candidate_id="gateway-" + ticket.id(),
                                                   operation_id=operation + ":candidate")
            services.evaluate(candidate, ticket.evaluation_id, operation_id=operation + ":evaluation", inputs=inputs)
            stage_id = self.spec.members[ticket.worker_id]
            evidence = services.evidence_view(stage_id, candidate_ids=(candidate.id,))
            attempt = None
            if self.spec.knowledge_space is not None:
                role = next(stage.role for stage in services.recipe.stages if stage.id == stage_id)
                outcome = services.knowledge(self.spec.knowledge_space).record_attempt(ticket.id(), evidence,
                    role=role, writer_id=ticket.worker_id, operation_id=operation)
                require(outcome.status == "published", "formal gateway attempt unexpectedly conflicted")
                attempt = outcome.revision
            result = GatewayResult(ticket=ticket, status="completed", candidate=candidate,
                                   evidence=evidence, shared_attempt=attempt)
        except RecoveryRequired:
            raise
        except Exception as exc:
            # Detailed evaluator failures remain private. A transport response
            # must not disclose task references or a grader's exception payload.
            result = GatewayResult(ticket=ticket, status="failed", candidate=candidate,
                                   diagnostic=type(exc).__name__ + ": formal evaluation did not complete")
        finally:
            if previous_worker is None:
                del services._worker_id
            else:
                services._worker_id = previous_worker

        def finish():
            saved = self.store.finish(result)
            services.values.publish("proteus.gateway.result@1", saved)
            return saved
        return services._record("gateway.finish", {"gateway_id": self.spec.id, "result": result},
                                finish, decode=GatewayResult.from_dict)

    def step(self, operation_id, *, inputs=None, timeout_seconds=0):
        ticket = self.claim(operation_id, timeout_seconds=timeout_seconds)
        return None if ticket is None else self.process(ticket, inputs=inputs)

    def response(self, worker_id, ticket_id):
        result = self.store.poll(worker_id, ticket_id)
        response = result.to_dict()
        if result.result is None or result.result.evidence is None:
            return response
        stage = next(item for item in self.services.recipe.stages if item.id == self.spec.members[worker_id])
        evidence = EvidenceStore(self.services.root / "evidence")
        view = evidence.verify(result.result.evidence)
        require((view.run_id, view.config_hash, view.lock_hash, view.stage_id, view.role, view.access) == (
            self.identity.run_id, self.identity.config_hash, self.identity.lock_hash, stage.id, stage.role, stage.evidence)
            and view.candidate_ids == (result.result.candidate.id,), "gateway response exceeds its evidence policy")
        response["view"] = view.to_dict()
        response["files"] = [FileWrite.from_bytes(item.path, evidence.read(result.result.evidence, item.path)).to_dict()
                             for item in view.entries]
        return response

    def serve(self):
        from proteus.gateway.transport import GatewayHost
        return GatewayHost(self)

    def knowledge(self):
        from proteus.gateway.knowledge import WorkerKnowledge
        return WorkerKnowledge(self)

    def connection(self, worker_id):
        token = self.store.credential(worker_id)
        endpoint = read_json((self.root / "endpoint.json").read_text())
        require(endpoint["identity_hash"] == content_hash(self.identity) and endpoint["host"] == "127.0.0.1"
                and type(endpoint["port"]) is int and 0 < endpoint["port"] < 65536,
                "gateway endpoint belongs to another run or transport")
        return {"PROTEUS_EVALUATION_URL": f"http://127.0.0.1:{endpoint['port']}", "PROTEUS_EVALUATION_TOKEN": token}


def validate_restored(services):
    values = [(ref, value) for ref, value in services.values._values.values()
              if ref.kind in ("proteus.gateway.ticket@1", "proteus.gateway.result@1")]
    if not values:
        return
    gateways = [GatewayService(services, spec.id) for spec in services.recipe.gateways]
    by_hash = {content_hash(gateway.identity): gateway for gateway in gateways}
    for ref, value in values:
        ticket = value if ref.kind == "proteus.gateway.ticket@1" else value.ticket
        require(ticket.identity_hash in by_hash, "restored gateway ticket belongs to an undeclared gateway")
        gateway = by_hash[ticket.identity_hash]
        polled = gateway.store.poll(ticket.worker_id, ticket.id())
        require(polled.ticket == ticket, "restored gateway ticket differs from its admission")
        services.artifacts.validate(ticket.artifact)
        if ref.kind == "proteus.gateway.result@1":
            require(polled.result == value, "restored gateway result differs from its response receipt")
            gateway.response(ticket.worker_id, ticket.id())
