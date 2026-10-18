"""Evidence access is a recipe declaration; a view is an immutable disclosure."""

from dataclasses import dataclass, field
from typing import Literal

from proteus.core.contracts import DIGEST, TEXT, UNIQUE, ArtifactComponent, DataRef, require, unique
from proteus.core.serialization import Contract, content_hash

EvidenceKind = Literal["candidate", "artifact", "task", "rollout", "trace", "feedback", "evaluation"]
KINDS = ("candidate", "artifact", "task", "rollout", "trace", "feedback", "evaluation")


@dataclass(frozen=True, kw_only=True)
class EvidenceAccess(Contract):
    evaluation_ids: tuple[str, ...] = field(metadata=UNIQUE)
    splits: tuple[str, ...] = field(metadata=UNIQUE)
    kinds: tuple[EvidenceKind, ...] = field(default=KINDS, metadata={**UNIQUE, "minItems": 1})

    def _validate(self):
        require(all(item.strip() for item in (*self.evaluation_ids, *self.splits)), "empty evidence scope")
        if set(self.kinds) - {"candidate", "artifact"}:
            require(bool(self.evaluation_ids) and bool(self.splits), "evaluation evidence needs explicit IDs and splits")


@dataclass(frozen=True, kw_only=True)
class EvidenceEntry(Contract):
    path: str = field(metadata=TEXT)
    kind: EvidenceKind
    candidate_id: str = field(metadata=TEXT)
    content_hash: str = field(metadata=DIGEST)
    size_bytes: int = field(metadata={"minimum": 0})
    source_hash: str = field(metadata=DIGEST)
    evaluation_id: str | None = field(default=None, metadata=TEXT)
    operation_id: str | None = field(default=None, metadata=TEXT)
    split: str | None = field(default=None, metadata=TEXT)
    rollout_id: str | None = field(default=None, metadata=TEXT)

    def _validate(self):
        ArtifactComponent(path=self.path, content_hash=self.content_hash, size_bytes=self.size_bytes)
        require(self.path != "view.json", "view.json is reserved for the evidence manifest")
        evaluation = self.kind not in ("candidate", "artifact")
        require(all((value is not None) == evaluation for value in
                    (self.evaluation_id, self.operation_id, self.split)), "evidence provenance is incomplete")
        require((self.rollout_id is not None) == (self.kind in ("task", "rollout", "trace", "feedback")),
                "evidence rollout provenance differs from kind")


@dataclass(frozen=True, kw_only=True)
class EvidenceView(Contract):
    run_id: str = field(metadata=TEXT)
    config_hash: str = field(metadata=DIGEST)
    lock_hash: str | None = field(metadata=DIGEST)
    stage_id: str = field(metadata=TEXT)
    role: str = field(metadata=TEXT)
    access: EvidenceAccess
    candidate_ids: tuple[str, ...] = field(metadata=UNIQUE)
    entries: tuple[EvidenceEntry, ...]

    def _validate(self):
        unique((entry.path.casefold() for entry in self.entries), "evidence file")
        require(tuple(entry.path for entry in self.entries) == tuple(sorted(entry.path for entry in self.entries)),
                "evidence files must be sorted")
        for entry in self.entries:
            require(entry.kind in self.access.kinds and entry.candidate_id in self.candidate_ids,
                    "evidence exceeds the declared access or population")
            if entry.evaluation_id is not None:
                require(entry.evaluation_id in self.access.evaluation_ids and entry.split in self.access.splits,
                        "evidence exceeds the declared evaluation/split")

    def ref(self) -> DataRef:
        digest = content_hash(self)
        return DataRef(kind="proteus.evidence.view@1", id=content_hash(["proteus.evidence.view@1", digest]),
                       content_hash=digest)
