"""Bounded single-space asynchronous search; provider runtimes remain recipe components."""

from dataclasses import dataclass, field
from typing import Literal, Mapping

from proteus.core.contracts import TEXT, DataRef, require
from proteus.core.serialization import Contract
from proteus.workers.records import WorkerObservation, WorkerRef


@dataclass(frozen=True, kw_only=True)
class CoralMethodOptions(Contract):
    gateway: str = field(metadata=TEXT)
    event_policy: str = field(metadata=TEXT)
    bootstrap_prompt: str = field(metadata=TEXT)
    max_submissions: int = field(metadata={"minimum": 1})
    max_polls: int = field(default=1000, metadata={"minimum": 1})
    poll_seconds: float = field(default=1, metadata={"exclusiveMinimum": 0})
    stop_timeout_seconds: float = field(default=20, metadata={"exclusiveMinimum": 0})
    max_restarts_per_worker: int = field(default=3, metadata={"minimum": 0})
    worker_prompts: Mapping[str, str] = field(default_factory=dict)
    minimize: bool = False
    final_selector: str = field(default="final", metadata=TEXT)
    final_evaluation: str | None = field(default=None, metadata=TEXT)

    def _validate(self):
        require(all(key.strip() and value.strip() for key, value in self.worker_prompts.items()),
                "worker prompt overrides must be named and nonempty")


@dataclass(frozen=True, kw_only=True)
class CoralState(Contract):
    phase: Literal["initial", "search", "closing", "finalizing"] = "initial"
    polls: int = field(default=0, metadata={"minimum": 0})
    workers: Mapping[str, WorkerRef] = field(default_factory=dict)
    restarts: Mapping[str, int] = field(default_factory=dict)
    results: tuple[DataRef, ...] = ()
    decisions: tuple[DataRef, ...] = ()
    latest: Mapping[str, DataRef] = field(default_factory=dict)
    reason: Literal["submission_limit", "poll_limit", "worker_failure"] | None = None
    closure: DataRef | None = None
    cancelled: tuple[DataRef, ...] = ()


@dataclass(frozen=True, kw_only=True)
class CoralPoll(Contract):
    workers: Mapping[str, WorkerObservation]


@dataclass(frozen=True, kw_only=True)
class CoralSummary(Contract):
    results: tuple[DataRef, ...]
    decisions: tuple[DataRef, ...]
    closure: DataRef
    cancelled: tuple[DataRef, ...]
    workers: Mapping[str, DataRef]
    restarts: Mapping[str, int]
    selection: DataRef
    final_evaluations: tuple[DataRef, ...]
    knowledge: DataRef
    polls: int = field(metadata={"minimum": 0})
    reason: Literal["submission_limit", "poll_limit", "worker_failure"]
