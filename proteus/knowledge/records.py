"""Research knowledge is distinct from candidates, handoffs and private grades."""

from dataclasses import dataclass, field
from typing import Literal

from proteus.core.contracts import DIGEST, TEXT, ArtifactComponent, ArtifactRef, DataRef, require, unique
from proteus.core.serialization import Contract, content_hash
from proteus.evidence.records import EvidenceAccess

KnowledgeKind = Literal["notes", "skills", "attempts"]
REVISION_KIND = "proteus.knowledge.revision@1"
SNAPSHOT_KIND = "proteus.knowledge.snapshot@1"


def validate_key(key):
    ArtifactComponent(path=key, content_hash=content_hash("path"), size_bytes=0)


def revision_ref(ref):
    require(ref.kind == REVISION_KIND and ref.id == content_hash([REVISION_KIND, ref.content_hash]),
            "not a knowledge revision reference")


@dataclass(frozen=True, kw_only=True)
class KnowledgeIdentity(Contract):
    run_id: str = field(metadata=TEXT)
    config_hash: str = field(metadata=DIGEST)
    lock_hash: str | None = field(metadata=DIGEST)
    evidence_access: EvidenceAccess | None = None
    space_id: str = field(default="shared", metadata=TEXT)


@dataclass(frozen=True, kw_only=True)
class KnowledgeWrite(Contract):
    kind: KnowledgeKind
    key: str = field(metadata=TEXT)
    writer_id: str = field(metadata=TEXT)
    operation_id: str = field(metadata=TEXT)
    expected: DataRef | None
    artifact: ArtifactRef | None
    sources: tuple[DataRef, ...] = ()
    writer_role: str | None = field(default=None, metadata=TEXT)

    def _validate(self):
        validate_key(self.key)
        if self.expected is not None:
            revision_ref(self.expected)
        require(self.artifact is not None or self.expected is not None, "deletion needs an existing revision")
        require(self.kind != "attempts" or (self.expected is None and self.artifact is not None),
                "formal attempts are append-only")
        unique((content_hash(ref) for ref in self.sources), "knowledge source")


@dataclass(frozen=True, kw_only=True)
class KnowledgeRevision(Contract):
    identity_hash: str = field(metadata=DIGEST)
    revision: int = field(metadata={"minimum": 1})
    write: KnowledgeWrite

    def ref(self):
        digest = content_hash(self)
        return DataRef(kind=REVISION_KIND, id=content_hash([REVISION_KIND, digest]), content_hash=digest)


@dataclass(frozen=True, kw_only=True)
class KnowledgeOutcome(Contract):
    status: Literal["published", "conflict"]
    revision: DataRef | None = None
    current: DataRef | None = None

    def _validate(self):
        require((self.status == "published") == (self.revision is not None), "knowledge outcome differs from status")
        require(self.status == "conflict" or self.current is None, "successful write cannot contain a conflict")
        for ref in (self.revision, self.current):
            if ref is not None:
                revision_ref(ref)


@dataclass(frozen=True, kw_only=True)
class KnowledgeSnapshot(Contract):
    identity: KnowledgeIdentity
    revisions: tuple[DataRef, ...]

    def _validate(self):
        for ref in self.revisions:
            revision_ref(ref)
        unique((ref.content_hash for ref in self.revisions), "snapshot revision")

    def ref(self):
        digest = content_hash(self)
        return DataRef(kind=SNAPSHOT_KIND, id=content_hash([SNAPSHOT_KIND, digest]), content_hash=digest)
