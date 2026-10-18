"""Pure policy inputs and durable decisions; scores originate in released evidence."""

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Protocol

from proteus.core.contracts import DIGEST, NONNEGATIVE, TEXT, DataRef, PluginRef, require, unique
from proteus.core.serialization import Contract
from proteus.workers.records import WorkerRef


@dataclass(frozen=True, kw_only=True)
class ClockSample(Contract):
    invocation: str = field(metadata=TEXT)
    monotonic_seconds: float = field(metadata=NONNEGATIVE)


@dataclass(frozen=True, kw_only=True)
class EventObservation(Contract):
    source_key: str = field(metadata=DIGEST)
    worker_id: str = field(metadata=TEXT)
    kind: Literal["evaluation", "tick"]
    clock: ClockSample
    result: DataRef | None = None
    score: float | None = None
    plateau_sample: bool = False

    def _validate(self):
        require((self.kind == "evaluation") == (self.result is not None), "evaluation event needs a formal result")
        if self.result is not None:
            require(self.result.kind == "proteus.gateway.result@1", "event needs a gateway result reference")
        require(self.plateau_sample or self.score is None, "non-sample event cannot contain a plateau score")
        require(self.kind != "tick" or not self.plateau_sample, "clock ticks cannot extend a score history")


@dataclass(frozen=True, kw_only=True)
class EventAction(Contract):
    name: str = field(metadata=TEXT)
    worker_id: str = field(metadata=TEXT)
    prompt: str = field(metadata=TEXT)


@dataclass(frozen=True)
class Transition:
    state: Contract
    actions: tuple[EventAction, ...] = ()


class EventPolicy(Protocol):
    """Implementations are pure: all changing inputs/state cross this boundary."""

    def initial_state(self) -> Contract: ...
    def advance(self, observation: EventObservation, state: Contract) -> Transition: ...


@dataclass(frozen=True, kw_only=True)
class EventDecision(Contract):
    policy_id: str = field(metadata=TEXT)
    identity_hash: str = field(metadata=DIGEST)
    plugin: PluginRef
    state_version: int = field(metadata={"minimum": 1})
    sequence: int = field(metadata={"minimum": 1})
    previous: DataRef | None
    state_before_hash: str = field(metadata=DIGEST)
    observation: EventObservation
    payload: Mapping[str, Any]
    actions: tuple[EventAction, ...]

    def _validate(self):
        require((self.sequence == 1) == (self.previous is None), "event decision chain is incomplete")
        require(self.previous is None or self.previous.kind == "proteus.event.decision@1", "invalid prior decision")
        unique(((item.worker_id, item.name) for item in self.actions), "event action")


@dataclass(frozen=True, kw_only=True)
class InterventionPlan(Contract):
    decision: DataRef
    worker_id: str = field(metadata=TEXT)
    previous: WorkerRef
    actions: tuple[EventAction, ...] = field(metadata={"minItems": 1})
    resume_policy: Literal["native", "fresh"]
    fresh_orientation: str | None = field(default=None, metadata=TEXT)

    def _validate(self):
        require(self.decision.kind == "proteus.event.decision@1", "intervention needs an event decision")
        require(self.previous.worker_id == self.worker_id and all(
            item.worker_id == self.worker_id for item in self.actions), "intervention belongs to another worker")
        require((self.resume_policy == "fresh") == (self.fresh_orientation is not None),
                "fresh continuation requires explicit orientation")


@dataclass(frozen=True, kw_only=True)
class InterventionDispatch(Contract):
    plan: DataRef
    worker: WorkerRef

    def _validate(self):
        require(self.plan.kind == "proteus.event.plan@1", "intervention dispatch needs a plan")
