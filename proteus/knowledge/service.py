"""Method operations and stage reads over explicitly declared shared spaces."""

from proteus.artifacts.codecs import components
from proteus.core.contracts import DataRef, require
from proteus.core.serialization import content_hash
from proteus.evidence.service import EvidenceService
from proteus.knowledge.records import REVISION_KIND, SNAPSHOT_KIND, KnowledgeIdentity, KnowledgeOutcome
from proteus.knowledge.store import KnowledgeConflict, SharedKnowledgeStore


class KnowledgeService:
    def __init__(self, services, space_id):
        self.services = services
        self.spec = next((item for item in services.recipe.knowledge if item.id == space_id), None)
        require(self.spec is not None, "unknown declared knowledge space")
        self.identity = KnowledgeIdentity(run_id=services.run_id, config_hash=services.recipe.config_hash,
            lock_hash=services.lock_hash, evidence_access=self.spec.evidence, space_id=space_id)
        self._store = None

    @property
    def store(self):
        if self._store is None:
            self._store = SharedKnowledgeStore(self.services.root / "private/knowledge" / content_hash(self.spec.id).split(":")[1],
                self.services.registry, identity=self.identity, evidence=EvidenceService(self.services).store)
        return self._store

    def _principal(self, role, identity, operation_id):
        require(role in self.spec.roles, "role cannot access this knowledge space")
        require(all(type(value) is str and value.strip() for value in (identity, operation_id)),
                "knowledge operations need principal and operation identities")

    def _sources(self, sources):
        for ref in sources:
            if ref.kind == REVISION_KIND:
                self.store.get(ref)
            else:
                require(ref.kind == "proteus.evidence.view@1", "knowledge source must be a revision or released evidence")
                view = self.services.values.get(ref)
                EvidenceService(self.services).bound(view, view.stage_id, verify_files=True)

    def _write(self, verb, role, writer_id, operation_id, inputs, action):
        self._principal(role, writer_id, operation_id)

        def apply():
            try:
                outcome = KnowledgeOutcome(status="published", revision=action())
            except KnowledgeConflict as exc:
                outcome = KnowledgeOutcome(status="conflict", current=exc.current)
            for ref in (outcome.revision, outcome.current):
                if ref is not None:
                    require(self.services.values.publish(REVISION_KIND, self.store.get(ref)) == ref,
                            "knowledge revision reference differs")
            return outcome

        outcome = self.services._record("knowledge." + verb,
            {"space_id": self.spec.id, "role": role, "writer_id": writer_id, "operation_id": operation_id, **inputs},
            apply, decode=KnowledgeOutcome.from_dict)
        for ref in (outcome.revision, outcome.current):
            if ref is not None:
                require(self.services.values.get(ref) == self.store.get(ref), "recorded knowledge revision changed")
        return outcome

    def publish(self, kind, key, files, *, role, writer_id, operation_id, expected=None, sources=()):
        self._principal(role, writer_id, operation_id)
        self._sources(sources)
        return self._write("publish", role, writer_id, operation_id,
            {"kind": kind, "key": key, "files": components(files), "expected": expected, "sources": sources},
            lambda: self.store.publish(kind, key, files, writer_id=writer_id, writer_role=role,
                operation_id=operation_id, expected=expected, sources=sources))

    def delete(self, kind, key, *, role, writer_id, operation_id, expected, sources=()):
        self._principal(role, writer_id, operation_id)
        self._sources(sources)
        return self._write("delete", role, writer_id, operation_id,
            {"kind": kind, "key": key, "expected": expected, "sources": sources},
            lambda: self.store.delete(kind, key, writer_id=writer_id, writer_role=role,
                operation_id=operation_id, expected=expected, sources=sources))

    def record_attempt(self, key, evidence, *, role, writer_id, operation_id):
        self._principal(role, writer_id, operation_id)
        self._sources((evidence,))
        return self._write("attempt", role, writer_id, operation_id, {"key": key, "evidence": evidence},
            lambda: self.store.record_attempt(key, evidence, writer_id=writer_id, writer_role=role, operation_id=operation_id))

    def capture(self, *, role, reader_id, operation_id):
        self._principal(role, reader_id, operation_id)

        def capture():
            snapshot = self.store.snapshot(reader_id=content_hash([role, reader_id]), operation_id=operation_id)
            return self.services.values.publish(SNAPSHOT_KIND, snapshot)

        ref = self.services._record("knowledge.capture", {"space_id": self.spec.id, "role": role,
            "reader_id": reader_id, "operation_id": operation_id}, capture, decode=DataRef.from_dict)
        self.bound(self.services.values.get(ref))
        return ref

    def bound(self, snapshot):
        require(snapshot.identity == self.identity, "knowledge snapshot belongs to another space or policy")
        ref = snapshot.ref()
        require(self.services.values.get(ref) == snapshot and self.store.get_snapshot(ref) == snapshot,
                "knowledge snapshot differs from its recorded value")
        self.store.records(snapshot)
        return ref


def validate_restored(services):
    values = [(ref, value) for ref, value in services.values._values.values()
              if ref.kind in (REVISION_KIND, SNAPSHOT_KIND)]
    if not values:
        return
    spaces = [KnowledgeService(services, spec.id) for spec in services.recipe.knowledge]
    by_hash = {content_hash(space.identity): space for space in spaces}
    for ref, value in values:
        identity_hash = value.identity_hash if ref.kind == REVISION_KIND else content_hash(value.identity)
        require(identity_hash in by_hash, "restored knowledge belongs to an undeclared space")
        space = by_hash[identity_hash]
        if ref.kind == REVISION_KIND:
            require(space.store.get(ref) == value, "restored knowledge revision differs from store")
        else:
            space.bound(value)
