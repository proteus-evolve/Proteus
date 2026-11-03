"""The text-classification Meta-Harness profile and its recorded decisions."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from proteus.core.contracts import TEXT, UNIQUE, DataRef, require, unique
from proteus.core.serialization import Contract
from proteus.execution.code_records import CodeState

REVISION = "8123ccabe2b19fa1123090b2e5f1bacc20963ce8"
REPOSITORY = "https://github.com/stanford-iris-lab/meta-harness"


@dataclass(frozen=True, kw_only=True)
class DatasetProfile(Contract):
    name: str = field(metadata=TEXT)
    train: str = field(metadata=TEXT)
    validation: str = field(metadata=TEXT)
    test: str = field(metadata=TEXT)
    seed: int = 42


@dataclass(frozen=True, kw_only=True)
class MetaOptions(Contract):
    datasets: tuple[DatasetProfile, ...] = field(metadata={"minItems": 1})
    proposer_stage: str = field(default="propose", metadata=TEXT)
    baselines: tuple[str, ...] = field(
        default=("no_memory", "fewshot_all"), metadata={**UNIQUE, "minItems": 1}
    )
    iterations: int = field(default=1, metadata={"minimum": 0})
    mode: Literal["offline", "online"] = "offline"
    num_epochs: int = field(default=1, metadata={"minimum": 1})
    batch_size: int = field(default=1, metadata={"minimum": 1})
    finalize: bool = True

    def _validate(self):
        unique(((d.name, d.seed) for d in self.datasets), "dataset/seed")
        require(
            self.mode == "offline" or self.num_epochs == 1, "online learning uses a single pass"
        )
        require(
            all(name.isidentifier() and not name.startswith("_") for name in self.baselines),
            "baseline names must be public module identifiers",
        )


@dataclass(frozen=True, kw_only=True)
class BenchmarkResult(Contract):
    system: str
    candidate_id: str
    dataset: str
    seed: int
    memory: CodeState
    complete: bool = False
    correct: int | None = None
    total: int | None = None
    context_chars: int | None = None
    validation: DataRef | None = None
    epoch_validations: tuple[DataRef, ...] = ()
    learning: tuple[DataRef, ...] = ()
    training: tuple[DataRef, ...] = ()
    diagnostic: str = ""

    def _validate(self):
        if self.complete:
            require(
                self.total is not None
                and self.total > 0
                and self.correct is not None
                and 0 <= self.correct <= self.total
                and self.context_chars is not None
                and self.context_chars >= 0,
                "complete benchmark needs valid counts and context length",
            )
        else:
            require(
                self.correct is None and self.total is None and self.context_chars is None,
                "incomplete benchmark cannot publish an aggregate score",
            )


@dataclass(frozen=True, kw_only=True)
class MetaSummary(Contract):
    benchmarks: tuple[DataRef, ...]
    iterations: tuple[DataRef, ...]
    frontier: Mapping[str, Any]
    selected: tuple[str, ...]
    tests: Mapping[str, DataRef]
    finalized: bool
    revision: str = REVISION


@dataclass(frozen=True, kw_only=True)
class IterationResult(Contract):
    iteration: int
    proposer: DataRef | None
    proposals: tuple[Mapping[str, Any], ...]
    diagnostics: tuple[str, ...]
    frontier: Mapping[str, Any]


def frontier(results, datasets):
    """Pinned benchmark aggregation: complete seed groups, accuracy vs context chars."""
    unique(((r.system, r.dataset, r.seed) for r in results), "benchmark group")
    groups = {}
    for result in results:
        groups.setdefault((result.system, result.dataset), {})[result.seed] = result
    required = {d.name: {p.seed for p in datasets if p.name == d.name} for d in datasets}
    aggregates = {}
    for (system, dataset), seeds in groups.items():
        if (
            dataset not in required
            or set(seeds) != required[dataset]
            or not all(r.complete for r in seeds.values())
        ):
            continue
        total = sum(r.total for r in seeds.values())
        aggregates[system, dataset] = (
            sum(r.correct for r in seeds.values()) / total,
            sum(r.context_chars for r in seeds.values()) / len(seeds),
        )
    # A failed dataset cannot silently improve a system's cross-dataset mean.
    complete_systems = {
        system for system, _ in aggregates if all((system, name) in aggregates for name in required)
    }
    by_dataset = {}
    for name in required:
        points = [(system, *aggregates[system, name]) for system in sorted(complete_systems)]
        if points:
            system, accuracy, context = max(points, key=lambda p: (p[1], -p[2]))
            by_dataset[name] = {
                "best_system": system,
                "accuracy": accuracy * 100,
                "ctx_len": context,
            }
    points = []
    for system in sorted(complete_systems):
        values = [aggregates[system, name] for name in required]
        positive = [context for _, context in values if context > 0]
        context = int(sum(positive) / len(positive)) if positive else 0
        points.append(
            (system, sum(accuracy for accuracy, _ in values) / len(values) * 100, context)
        )
    pareto = [
        point
        for point in points
        if not any(
            other[1] >= point[1]
            and other[2] <= point[2]
            and (other[1] > point[1] or other[2] < point[2])
            for other in points
        )
    ]
    by_dataset["_pareto"] = [
        {"system": system, "val_accuracy": round(accuracy, 1), "ctx_len": context}
        for system, accuracy, context in sorted(pareto, key=lambda p: (-p[1], p[2]))
    ]
    return by_dataset


def final_systems(baselines, selection):
    return tuple(
        sorted(
            {
                *baselines,
                *(r["system"] for r in selection["_pareto"]),
                *(r["best_system"] for key, r in selection.items() if not key.startswith("_")),
            }
        )
    )
