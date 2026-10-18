"""Pinned task data and public batches; references never travel to subject runners."""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from proteus.core.contracts import DIGEST, TEXT, PluginRef, require, unique
from proteus.core.serialization import Contract, content_hash
from proteus.recipes.config import TaskSuiteSpec


@dataclass(frozen=True, kw_only=True)
class StateValue(Contract):
    payload: Any = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class Task(Contract):
    id: str = field(metadata=TEXT)
    version: str = field(metadata=TEXT)
    split: str = field(metadata=TEXT)
    input: Any


@dataclass(frozen=True, kw_only=True)
class TaskRecord(Contract):
    task: Task
    reference: Any


@dataclass(frozen=True, kw_only=True)
class TaskDataset(Contract):
    version: str = field(metadata=TEXT)
    records: tuple[TaskRecord, ...] = field(metadata={"minItems": 1})
    initial_state: StateValue = field(default_factory=StateValue)

    def _validate(self):
        unique(((record.task.split, record.task.id) for record in self.records), "task identity within split")


@dataclass(frozen=True, kw_only=True)
class TaskBatch(Contract):
    suite: PluginRef
    dataset_version: str = field(metadata=TEXT)
    dataset_hash: str = field(metadata=DIGEST)
    split: str = field(metadata=TEXT)
    order: Literal["declared", "shuffle"]
    seed: int
    tasks: tuple[Task, ...] = field(metadata={"minItems": 1})
    initial_state: StateValue

    def _validate(self):
        unique((task.id for task in self.tasks), "batch task")
        require(all(task.split == self.split for task in self.tasks), "batch mixes task splits")


class TaskSuite(Protocol):
    def load(self) -> TaskDataset: ...


@dataclass(frozen=True, kw_only=True)
class JsonSuiteOptions(Contract):
    path: str = field(metadata=TEXT)


class JsonTaskSuite:
    def __init__(self, options):
        self.options = options

    def load(self):
        return TaskDataset.from_json(Path(self.options.path).read_text(encoding="utf-8"))


def make_batch(dataset: TaskDataset, spec: TaskSuiteSpec) -> TaskBatch:
    require(type(dataset) is TaskDataset, "task suite must return TaskDataset")
    require(spec.dataset_hash is not None and content_hash(dataset) == spec.dataset_hash,
            "task data differs from its declared content hash")
    require(dataset.version == spec.dataset_version, "task data version mismatch")
    available = {record.task.id: record.task for record in dataset.records if record.task.split == spec.split}
    require(set(spec.task_ids) <= set(available), "requested task ID is absent from the declared split")
    tasks = [available[key] for key in spec.task_ids]
    if spec.order == "shuffle":
        random.Random(spec.seed).shuffle(tasks)
    return TaskBatch(suite=spec.suite.plugin, dataset_version=dataset.version, dataset_hash=spec.dataset_hash,
                     split=spec.split, order=spec.order, seed=spec.seed, tasks=tuple(tasks),
                     initial_state=dataset.initial_state)
