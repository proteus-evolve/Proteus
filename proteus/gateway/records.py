"""Candidate submissions carry bytes and provenance, never worker-supplied grades."""

from dataclasses import dataclass, field
from typing import Literal

from proteus.artifacts.codecs import FileWrite, components
from proteus.core.contracts import DIGEST, TEXT, ArtifactRef, Candidate, DataRef, require, unique
from proteus.core.serialization import Contract, content_hash


@dataclass(frozen=True, kw_only=True)
class GatewayIdentity(Contract):
    run_id: str = field(metadata=TEXT)
    config_hash: str = field(metadata=DIGEST)
    lock_hash: str | None = field(metadata=DIGEST)
    gateway_id: str = field(metadata=TEXT)


@dataclass(frozen=True, kw_only=True)
class GatewaySubmission(Contract):
    operation_id: str = field(metadata=TEXT)
    evaluation_id: str = field(metadata=TEXT)
    files: tuple[FileWrite, ...] = field(metadata={"minItems": 1})

    def _validate(self):
        unique((item.path for item in self.files), "submission file")
        components({item.path: item.content() for item in self.files})


@dataclass(frozen=True, kw_only=True)
class GatewayTicket(Contract):
    identity_hash: str = field(metadata=DIGEST)
    worker_id: str = field(metadata=TEXT)
    operation_id: str = field(metadata=TEXT)
    request_hash: str = field(metadata=DIGEST)
    artifact: ArtifactRef
    evaluation_id: str = field(metadata=TEXT)
    task_count: int = field(metadata={"minimum": 1})

    def id(self):
        return content_hash(self).split(":")[1]


@dataclass(frozen=True, kw_only=True)
class GatewayResult(Contract):
    ticket: GatewayTicket
    status: Literal["completed", "failed"]
    candidate: Candidate | None = None
    evidence: DataRef | None = None
    shared_attempt: DataRef | None = None
    diagnostic: str | None = None

    def _validate(self):
        if self.status == "completed":
            require(self.candidate is not None and self.evidence is not None and self.diagnostic is None,
                    "completed gateway evaluation requires a candidate and released evidence")
        else:
            require(self.evidence is None and self.shared_attempt is None and self.diagnostic is not None,
                    "failed gateway evaluation needs a diagnostic without fabricated feedback")
        if self.candidate is not None:
            require(self.candidate.artifact == self.ticket.artifact, "gateway candidate differs from submitted bytes")
        if self.evidence is not None:
            require(self.evidence.kind == "proteus.evidence.view@1", "gateway feedback must be released evidence")
        if self.shared_attempt is not None:
            require(self.shared_attempt.kind == "proteus.knowledge.revision@1", "invalid shared attempt reference")


@dataclass(frozen=True, kw_only=True)
class GatewayClaim(Contract):
    ticket: GatewayTicket | None = None


@dataclass(frozen=True, kw_only=True)
class GatewayPoll(Contract):
    ticket: GatewayTicket
    status: Literal["queued", "processing", "completed", "failed"]
    result: GatewayResult | None = None

    def _validate(self):
        require((self.status in ("completed", "failed")) == (self.result is not None), "gateway result status differs")
        if self.result is not None:
            require(self.result.ticket == self.ticket and self.result.status == self.status, "gateway result belongs to another ticket")


@dataclass(frozen=True, kw_only=True)
class GatewayClosure(Contract):
    identity_hash: str = field(metadata=DIGEST)
    operation_id: str = field(metadata=TEXT)
    reason: Literal["completed", "submission_limit", "poll_limit", "worker_failure", "requested"]
    pending: tuple[GatewayTicket, ...]

    def _validate(self):
        unique((ticket.id() for ticket in self.pending), "pending gateway ticket")
        require(all(ticket.identity_hash == self.identity_hash for ticket in self.pending),
                "gateway closure contains a foreign ticket")
