"""Authenticated worker knowledge operations independent of method replay.

These operations use the store's atomic receipts directly. They never mutate
RunServices.Values or dispatch the method journal from an HTTP handler thread.
"""

from dataclasses import dataclass, field
import re
from typing import Literal

from proteus.artifacts.codecs import FileWrite, components
from proteus.core.contracts import TEXT, DataRef, require, unique
from proteus.core.serialization import Contract, content_hash
from proteus.knowledge.records import REVISION_KIND, KnowledgeOutcome, validate_key
from proteus.knowledge.store import KnowledgeConflict


@dataclass(frozen=True, kw_only=True)
class KnowledgeCapture(Contract):
    operation_id: str = field(metadata=TEXT)


@dataclass(frozen=True, kw_only=True)
class KnowledgeSubmission(Contract):
    operation_id: str = field(metadata=TEXT)
    action: Literal["publish", "delete"]
    kind: Literal["notes", "skills"]
    key: str = field(metadata=TEXT)
    files: tuple[FileWrite, ...] = ()
    expected: DataRef | None = None
    sources: tuple[DataRef, ...] = ()

    def _validate(self):
        validate_key(self.key)
        unique((item.path for item in self.files), "knowledge upload file")
        components({item.path: item.content() for item in self.files})
        if self.action == "publish":
            require(bool(self.files), "knowledge publication requires files")
        else:
            require(not self.files and self.expected is not None, "knowledge deletion needs an expected revision and no files")


def revision_ref(digest):
    require(re.fullmatch(r"[0-9a-f]{64}", digest) is not None, "invalid knowledge revision digest")
    digest = "sha256:" + digest
    return DataRef(kind=REVISION_KIND, content_hash=digest, id=content_hash([REVISION_KIND, digest]))


class WorkerKnowledge:
    def __init__(self, gateway):
        self.gateway = gateway

    def _access(self, worker, *, write=False):
        spec = self.gateway.spec
        require(worker in spec.members, "worker is not a gateway member")
        require(spec.knowledge_space is not None and spec.knowledge_access in
                (("read_write",) if write else ("read", "read_write")), "worker knowledge access is not granted")
        role = next(item.role for item in self.gateway.services.recipe.workers if item.id == worker)
        service = self.gateway.services.knowledge(spec.knowledge_space)
        require(role in service.spec.roles, "worker role cannot access this knowledge space")
        return service.store, role

    def capture(self, worker, request):
        store, role = self._access(worker)
        snapshot = store.snapshot(reader_id=content_hash(["gateway-worker", self.gateway.spec.id, role, worker]),
                                  operation_id=request.operation_id)
        return {"snapshot": snapshot.to_dict(), "ref": snapshot.ref().to_dict()}

    def read(self, worker, ref):
        store, _ = self._access(worker)
        record = store.get(ref)
        files = store.artifacts.get(record.write.artifact) if record.write.artifact is not None else {}
        return {"ref": ref.to_dict(), "revision": record.to_dict(),
                "files": [FileWrite.from_bytes(path, item.data, executable=item.executable).to_dict()
                          for path, item in sorted(files.items())]}

    def write(self, worker, request):
        store, role = self._access(worker, write=True)
        require(len(request.files) <= self.gateway.spec.max_files
                and len(request.to_json().encode()) <= self.gateway.spec.max_request_bytes,
                "worker knowledge submission exceeds configured limits")
        # Separate autonomous writes from controller-generated formal attempts:
        # workers cannot occupy the latter's operation namespace by choosing IDs.
        operation = "gateway-worker:" + content_hash([self.gateway.spec.id, request.operation_id])
        kwargs = dict(writer_id=worker, writer_role=role, operation_id=operation,
                      expected=request.expected, sources=request.sources)
        try:
            if request.action == "publish":
                ref = store.publish(request.kind, request.key, {item.path: item.content() for item in request.files}, **kwargs)
            else:
                ref = store.delete(request.kind, request.key, **kwargs)
            outcome = KnowledgeOutcome(status="published", revision=ref)
        except KnowledgeConflict as exc:
            outcome = KnowledgeOutcome(status="conflict", current=exc.current)
        # Return the exact historical receipt, including the old current revision
        # for a repeated conflict, even after a later successful publication.
        ref = outcome.revision or outcome.current
        return {"outcome": outcome.to_dict(), "record": self.read(worker, ref) if ref is not None else None}

    def dispatch(self, worker, method, path, body=None):
        if method == "POST" and path == "/v1/knowledge/snapshots":
            return self.capture(worker, KnowledgeCapture.from_dict(body))
        if method == "POST" and path == "/v1/knowledge/writes":
            return self.write(worker, KnowledgeSubmission.from_dict(body))
        match = re.fullmatch(r"/v1/knowledge/revisions/([0-9a-f]{64})", path)
        require(method == "GET" and match is not None, "unknown knowledge route")
        return self.read(worker, revision_ref(match[1]))
