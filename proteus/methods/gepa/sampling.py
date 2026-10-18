"""Checkpointable wrapper of the reference epoch-shuffled minibatch sampler.

This is a method building block. Its typed state must be included in MethodState
by the calling method; it is not a separate execution/recovery engine.
"""

from __future__ import annotations

import random
from collections import Counter
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from proteus.core.contracts import DIGEST, TEXT, require, unique
from proteus.core.serialization import Contract, content_hash
from proteus.evaluation.tasks import TaskBatch, TaskSelection
from proteus.methods.gepa.reference import load_reference


@dataclass(frozen=True, kw_only=True)
class Frequency(Contract):
    task_id: str = field(metadata=TEXT)
    count: int = field(metadata={"minimum": 1})


@dataclass(frozen=True, kw_only=True)
class SamplerState(Contract):
    pool_hash: str = field(metadata=DIGEST)
    minibatch_size: int = field(metadata={"minimum": 1})
    shuffled_ids: tuple[str, ...] = ()
    epoch: int = field(default=-1, metadata={"minimum": -1})
    # Counter insertion order matters to least-frequency padding ties. A sorted
    # JSON object would lose it, so frequencies are an ordered sequence.
    frequencies: tuple[Frequency, ...] = ()
    last_trainset_size: int = field(default=0, metadata={"minimum": 0})
    current_iteration: int | None = field(default=None, metadata={"minimum": 0})
    calls_in_iteration: int = field(default=0, metadata={"minimum": 0})
    rng_state: Any

    def _validate(self):
        unique((item.task_id for item in self.frequencies), "sampler frequency")
        try:
            random.Random().setstate(self.rng_state)
        except (TypeError, ValueError, OverflowError, IndexError) as exc:
            from proteus.core.serialization import ContractError
            raise ContractError("invalid sampler RNG state") from exc


class EpochSampler:
    def __init__(self, pool: TaskBatch, minibatch_size: int, rng: random.Random):
        require(type(pool) is TaskBatch and pool.selection is None and bool(pool.tasks),
                "GEPA sampler requires a nonempty original task pool")
        require(type(minibatch_size) is int and minibatch_size > 0, "minibatch_size must be positive")
        require(type(rng) is random.Random, "sampler requires an explicit shared Random generator")
        load_reference()
        from gepa.strategies.batch_sampler import EpochShuffledBatchSampler
        self.pool = pool
        self.ids = tuple(task.id for task in pool.tasks)
        self.sampler = EpochShuffledBatchSampler(minibatch_size, rng)

    def all_ids(self):
        return self.ids

    def __len__(self):
        return len(self.ids)

    def select(self, iteration: int) -> TaskSelection:
        require(type(iteration) is int and iteration >= 0, "sampler iteration must be nonnegative")
        previous = self.sampler._current_iteration
        require(previous is None or iteration >= previous, "sampler iteration cannot move backwards")
        return TaskSelection(task_ids=tuple(self.sampler.next_minibatch_ids(self, SimpleNamespace(i=iteration))))

    def snapshot(self) -> SamplerState:
        sampler = self.sampler
        return SamplerState(pool_hash=content_hash(self.pool), minibatch_size=sampler.minibatch_size,
            shuffled_ids=tuple(sampler.shuffled_ids), epoch=sampler.epoch,
            frequencies=tuple(Frequency(task_id=key, count=value) for key, value in sampler.id_freqs.items()),
            last_trainset_size=sampler.last_trainset_size, current_iteration=sampler._current_iteration,
            calls_in_iteration=sampler._calls_in_iteration, rng_state=sampler.rng.getstate())

    def restore(self, state: SamplerState):
        require(type(state) is SamplerState, "expected typed GEPA sampler state")
        sampler = self.sampler
        require(state.pool_hash == content_hash(self.pool) and state.minibatch_size == sampler.minibatch_size,
                "sampler state belongs to another pool or minibatch size")
        counts = {item.task_id: item.count for item in state.frequencies}
        require(Counter(state.shuffled_ids) == counts, "sampler frequencies differ from shuffled occurrences")
        if state.epoch == -1:
            require(not state.shuffled_ids and not state.frequencies and state.last_trainset_size == 0
                    and state.current_iteration is None and state.calls_in_iteration == 0,
                    "invalid initial sampler state")
        else:
            require(set(state.shuffled_ids) == set(self.ids) and state.last_trainset_size == len(self.ids)
                    and len(state.shuffled_ids) == ((len(self.ids) + sampler.minibatch_size - 1)
                                                   // sampler.minibatch_size) * sampler.minibatch_size
                    and state.current_iteration is not None, "invalid sampled pool state")
        # All validation precedes mutation, including RNG restoration.
        sampler.rng.setstate(state.rng_state)
        sampler.shuffled_ids = list(state.shuffled_ids)
        sampler.epoch = state.epoch
        sampler.id_freqs = Counter(counts)
        sampler.last_trainset_size = state.last_trainset_size
        sampler._current_iteration = state.current_iteration
        sampler._calls_in_iteration = state.calls_in_iteration
