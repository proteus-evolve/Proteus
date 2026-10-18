"""Worker identities and durable launch/completion records."""

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping

from proteus.core.contracts import DIGEST, TEXT, ArtifactRef, PluginRef, Usage, require
from proteus.core.serialization import Contract, content_hash


@dataclass(frozen=True, kw_only=True)
class CommandWorkerOptions(Contract):
    # This command is a trusted runtime bridge, not candidate-provided code.
    command: tuple[str, ...] = field(metadata={"minItems": 1})
    command_files: Mapping[str, str]
    credential_env: tuple[str, ...] = ()
    timeout_seconds: float = field(default=3600, metadata={"exclusiveMinimum": 0})
    shutdown_grace_seconds: float = field(default=10, metadata={"exclusiveMinimum": 0})

    def _validate(self):
        import re
        from pathlib import Path
        require(all(arg and "\0" not in arg for arg in self.command), "worker command arguments must be nonempty")
        require(Path(self.command[0]).is_absolute() and self.command[0] in self.command_files,
                "worker executable requires an absolute path and a content pin")
        require(all(Path(path).is_absolute() and re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
                    for path, digest in self.command_files.items()), "invalid worker command file pin")
        require(len(set(self.credential_env)) == len(self.credential_env) and all(
            re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) for name in self.credential_env),
            "worker credential names must be unique environment names")
        require(not set(self.credential_env) & {"PROTEUS_WORKER_REQUEST", "PYTHONPATH"},
                "worker credentials cannot replace protocol or Python import configuration")


@dataclass(frozen=True, kw_only=True)
class WorkerIdentity(Contract):
    run_id: str = field(metadata=TEXT)
    config_hash: str = field(metadata=DIGEST)
    lock_hash: str | None = field(metadata=DIGEST)
    worker_id: str = field(metadata=TEXT)
    role: str = field(metadata=TEXT)


@dataclass(frozen=True, kw_only=True)
class WorkerRef(Contract):
    worker_id: str = field(metadata=TEXT)
    operation_id: str = field(metadata=TEXT)
    request_hash: str = field(metadata=DIGEST)


@dataclass(frozen=True, kw_only=True)
class WorkerSeed(Contract):
    identity: WorkerIdentity
    operation_id: str = field(metadata=TEXT)
    artifact: ArtifactRef
    directory: str = field(metadata={"pattern": r"^[A-Za-z0-9][A-Za-z0-9._-]*$"})


@dataclass(frozen=True, kw_only=True)
class WorkerLaunch(Contract):
    identity: WorkerIdentity
    runtime: PluginRef
    options: Mapping[str, Any]
    operation_id: str = field(metadata=TEXT)
    prompt: str = field(metadata=TEXT)
    workspace: str = field(metadata=TEXT)
    previous: WorkerRef | None = None
    session_id: str | None = field(default=None, metadata=TEXT)
    continuity: Literal["fresh", "native", "fresh_fallback"] = "fresh"
    fallback_reason: str | None = field(default=None, metadata=TEXT)

    def _validate(self):
        require((self.continuity == "native") == (self.session_id is not None),
                "native continuation requires an exact session identity")
        require((self.continuity == "fresh") == (self.previous is None),
                "continuation must identify the preceding worker generation")
        require((self.continuity == "fresh_fallback") == (self.fallback_reason is not None),
                "fresh fallback needs an explicit reason")
        if self.previous is not None:
            require(self.previous.worker_id == self.identity.worker_id, "cannot resume another worker")

    def ref(self):
        return WorkerRef(worker_id=self.identity.worker_id, operation_id=self.operation_id,
                         request_hash=content_hash(self))


@dataclass(frozen=True, kw_only=True)
class WorkerCompletion(Contract):
    request_hash: str = field(metadata=DIGEST)
    stdout_hash: str = field(metadata=DIGEST)
    stderr_hash: str = field(metadata=DIGEST)
    status: Literal["completed", "failed", "paused", "stopped"]
    return_code: int | None
    session_id: str | None = field(default=None, metadata=TEXT)
    resumed_from: str | None = field(default=None, metadata=TEXT)
    usage: Usage = field(default_factory=Usage)
    diagnostic: str | None = None
    control: Literal["pause", "stop"] | None = None


@dataclass(frozen=True, kw_only=True)
class WorkerObservation(Contract):
    ref: WorkerRef
    state: Literal["pending", "active", "completed", "failed", "paused", "stopped", "uncertain"]
    completion: WorkerCompletion | None = None

    def _validate(self):
        require((self.state in ("completed", "failed", "paused", "stopped")) == (self.completion is not None),
                "terminal worker state requires its completion receipt")
        if self.completion is not None:
            require(self.completion.request_hash == self.ref.request_hash and self.completion.status == self.state,
                    "worker receipt differs from the requested generation")
