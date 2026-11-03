"""Checkpointable variable-size epoch traversal for single-proposal GEPA.

Changing size preserves the current shuffle's unvisited tail. Least-frequency
padding keeps a minibatch unique, following the pinned dynamic-sampler proposal.
The shared controller measures normal iterations through Proteus timing records.
"""

from collections import Counter
from dataclasses import dataclass, field
import random
from typing import Any

from proteus.core.contracts import DIGEST, require
from proteus.core.serialization import Contract, content_hash
from proteus.evaluation.tasks import TaskSelection
from proteus.methods.gepa.sampling import Frequency
from proteus.operators.combee.batching import (
    BatchControllerState, initialize, observe, validate_state,
)


@dataclass(frozen=True, kw_only=True)
class DynamicSamplerState(Contract):
    pool_hash: str = field(metadata=DIGEST)
    controller: BatchControllerState
    shuffled_ids: tuple[str, ...] = ()
    frequencies: tuple[Frequency, ...] = ()
    cursor: int = field(default=0, metadata={"minimum": 0})
    epoch: int = field(default=-1, metadata={"minimum": -1})
    last_iteration: int | None = field(default=None, metadata={"minimum": 0})
    sample_count: int = field(default=0, metadata={"minimum": 0})
    rng_state: Any

    def _validate(self):
        require(len(set(self.shuffled_ids)) == len(self.shuffled_ids), "dynamic shuffle must contain unique IDs")
        require(self.cursor <= len(self.shuffled_ids), "dynamic shuffle cursor is outside the epoch")
        require(len({row.task_id for row in self.frequencies}) == len(self.frequencies), "duplicate frequency")
        require({row.task_id for row in self.frequencies} == set(self.shuffled_ids), "dynamic frequencies differ from the epoch")
        require((self.epoch == -1) == (not self.shuffled_ids), "dynamic epoch inventory is inconsistent")
        if self.epoch == -1:
            require(self.last_iteration is None and self.cursor == 0 and self.sample_count == 0
                    and not self.controller.observations, "initial dynamic sampler contains learning state")
        else:
            require(self.last_iteration is not None and self.cursor > 0,
                    "sampled dynamic epoch requires an iteration and cursor")
        probe = random.Random()
        try:
            probe.setstate(self.rng_state)
        except (TypeError, ValueError, OverflowError, IndexError) as exc:
            from proteus.core.serialization import ContractError
            raise ContractError("invalid dynamic sampler RNG state") from exc


class DynamicEpochSampler:
    def __init__(self, pool, options, rng):
        require(pool.selection is None and bool(pool.tasks), "dynamic sampler requires an original nonempty pool")
        self.pool, self.options, self.rng = pool, options, rng
        self.ids = tuple(task.id for task in pool.tasks)
        self.controller = initialize(options, pool_hash=content_hash(pool), dataset_size=len(self.ids))
        self.shuffled_ids, self.frequencies = [], Counter()
        self.cursor, self.epoch, self.last_iteration, self.sample_count = 0, -1, None, 0

    def select(self, iteration):
        require(type(iteration) is int and iteration >= 0, "dynamic sampler iteration must be nonnegative")
        require(self.sample_count == 0 and (self.last_iteration is None or iteration > self.last_iteration),
                "this dynamic GEPA profile supports one minibatch per completed iteration")
        size = self.controller.size
        if self.cursor >= len(self.shuffled_ids):
            self.epoch += 1
            self.shuffled_ids = list(self.ids)
            self.rng.shuffle(self.shuffled_ids)
            self.frequencies = Counter(self.shuffled_ids)
            self.cursor = 0
        end = min(self.cursor + size, len(self.shuffled_ids))
        batch = self.shuffled_ids[self.cursor:end]
        self.cursor = end
        used = set(batch)
        while len(batch) < size:
            chosen = min((key for key in self.shuffled_ids if key not in used),
                         key=lambda key: self.frequencies[key])
            batch.append(chosen)
            used.add(chosen)
            self.frequencies[chosen] += 1
        self.last_iteration, self.sample_count = iteration, len(batch)
        return TaskSelection(task_ids=tuple(batch))

    def finish(self, measurement, elapsed):
        require(self.sample_count > 0, "dynamic iteration has no sampled tasks")
        self.controller = observe(self.options, self.controller, actual_size=self.sample_count,
                                  measurement=measurement, elapsed=elapsed)
        self.sample_count = 0

    def snapshot(self):
        return DynamicSamplerState(pool_hash=content_hash(self.pool), controller=self.controller,
            shuffled_ids=tuple(self.shuffled_ids),
            frequencies=tuple(Frequency(task_id=key, count=count) for key, count in self.frequencies.items()),
            cursor=self.cursor, epoch=self.epoch, last_iteration=self.last_iteration,
            sample_count=self.sample_count, rng_state=self.rng.getstate())

    def restore(self, state):
        require(type(state) is DynamicSamplerState and state.pool_hash == content_hash(self.pool),
                "dynamic sampler state belongs to another pool or sampler")
        validate_state(self.options, state.controller, pool_hash=content_hash(self.pool), dataset_size=len(self.ids))
        require(not state.shuffled_ids or set(state.shuffled_ids) == set(self.ids), "dynamic shuffle lost tasks")
        require(state.sample_count == 0, "dynamic sampler checkpoints must follow completed iterations")
        self.rng.setstate(state.rng_state)
        self.controller = state.controller
        self.shuffled_ids = list(state.shuffled_ids)
        self.frequencies = Counter({row.task_id: row.count for row in state.frequencies})
        self.cursor, self.epoch = state.cursor, state.epoch
        self.last_iteration, self.sample_count = state.last_iteration, state.sample_count
