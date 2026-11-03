"""Reusable count/time/stall rules with explicit, persisted cooldowns."""

from dataclasses import dataclass, field, replace
from typing import Literal, Mapping

from proteus.core.contracts import NONNEGATIVE, TEXT, require, unique
from proteus.core.serialization import Contract, content_hash
from proteus.events.records import ClockSample, EventAction, Transition


@dataclass(frozen=True, kw_only=True)
class CadenceRule(Contract):
    name: str = field(metadata=TEXT)
    trigger: Literal["evaluations", "time", "plateau"]
    every: float = field(metadata={"exclusiveMinimum": 0})
    prompt: str = field(metadata=TEXT)
    scope: Literal["worker", "global"] = "worker"
    epsilon: float = field(default=0, metadata=NONNEGATIVE)

    def _validate(self):
        require(self.trigger == "time" or self.every.is_integer(), "count/stall thresholds must be whole numbers")
        require(self.trigger == "plateau" or self.epsilon == 0, "epsilon only applies to plateaus")
        require(self.trigger != "plateau" or self.scope == "worker", "plateaus use each worker's own scores")


@dataclass(frozen=True, kw_only=True)
class CadenceOptions(Contract):
    rules: tuple[CadenceRule, ...] = field(metadata={"minItems": 1})
    minimize: bool = False

    def _validate(self):
        unique((rule.name for rule in self.rules), "cadence rule")


@dataclass(frozen=True, kw_only=True)
class Elapsed(Contract):
    seconds: float = field(default=0, metadata=NONNEGATIVE)
    gaps: int = field(default=0, metadata={"minimum": 0})
    last: ClockSample | None = None

    def advance(self, clock):
        if self.last is None:
            return replace(self, last=clock)
        if clock.invocation != self.last.invocation:
            return replace(self, last=clock, gaps=self.gaps + 1)
        require(clock.monotonic_seconds >= self.last.monotonic_seconds, "event monotonic clock moved backwards")
        return replace(self, seconds=self.seconds + clock.monotonic_seconds - self.last.monotonic_seconds, last=clock)


@dataclass(frozen=True, kw_only=True)
class WorkerProgress(Contract):
    evaluations: int = field(default=0, metadata={"minimum": 0})
    scores: tuple[float | None, ...] = ()
    elapsed: Elapsed = field(default_factory=Elapsed)


@dataclass(frozen=True, kw_only=True)
class CadenceState(Contract):
    evaluations: int = field(default=0, metadata={"minimum": 0})
    workers: Mapping[str, WorkerProgress] = field(default_factory=dict)
    elapsed: Elapsed = field(default_factory=Elapsed)
    fired: Mapping[str, float] = field(default_factory=dict)


def progress(state, observation):
    old = state.workers.get(observation.worker_id, WorkerProgress())
    count = int(observation.kind == "evaluation")
    current = replace(old, evaluations=old.evaluations + count,
        scores=old.scores + ((observation.score,) if observation.plateau_sample else ()),
        elapsed=old.elapsed.advance(observation.clock))
    return replace(state, evaluations=state.evaluations + count,
        workers={**state.workers, observation.worker_id: current}, elapsed=state.elapsed.advance(observation.clock))


def plateau_streak(scores, *, minimize, epsilon):
    anchor, streak = None, 0
    for score in scores:
        if score is None:
            if anchor is not None:
                streak += 1
        elif anchor is None or (score < anchor - epsilon if minimize else score > anchor + epsilon):
            anchor, streak = score, 0
        else:
            streak += 1
    return streak


class CadencePolicy:
    def __init__(self, options):
        self.options = options

    def initial_state(self):
        return CadenceState()

    def advance(self, observation, state):
        state = progress(state, observation)
        worker = state.workers[observation.worker_id]
        fired, actions = dict(state.fired), []
        for rule in self.options.rules:
            key = content_hash([rule.name, observation.worker_id if rule.scope == "worker" else None])
            active = False
            if rule.trigger == "evaluations" and observation.kind == "evaluation":
                count = worker.evaluations if rule.scope == "worker" else state.evaluations
                active = count > 0 and count % int(rule.every) == 0
            elif rule.trigger == "time":
                elapsed = worker.elapsed if rule.scope == "worker" else state.elapsed
                active = elapsed.seconds - fired.get(key, 0) >= rule.every
                if active:
                    fired[key] = elapsed.seconds
            elif rule.trigger == "plateau" and observation.plateau_sample:
                streak = plateau_streak(worker.scores, minimize=self.options.minimize, epsilon=rule.epsilon)
                if streak == 0:
                    fired.pop(key, None)
                active = streak >= rule.every and streak - fired.get(key, 0) >= rule.every
                if active:
                    fired[key] = streak
            if active:
                actions.append(EventAction(name=rule.name, worker_id=observation.worker_id, prompt=rule.prompt))
        return Transition(replace(state, fired=fired), tuple(actions))
