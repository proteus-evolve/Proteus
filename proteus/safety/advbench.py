"""AdvBench harmful-behavior rows used as memory-injection payloads."""

from __future__ import annotations

import csv
import os
import random
from dataclasses import dataclass
from pathlib import Path

from proteus.bench._datasets import download_verified

DATA_URL = (
    "https://raw.githubusercontent.com/llm-attacks/llm-attacks/"
    "main/data/advbench/harmful_behaviors.csv"
)
DATA_SHA256 = "6cd1a5c63c07610d7eb67307772ee5606017ee950b5770ab288a2c487489d3e1"
SAMPLE_SIZE = 16
SAMPLE_SIZE_MIN = 10
SAMPLE_SIZE_MAX = 20


@dataclass(frozen=True)
class AdvBenchItem:
    goal: str
    target: str
    index: int


def dataset_path(dataset_file: str | os.PathLike | None = None) -> Path:
    if dataset_file:
        return Path(dataset_file).expanduser()
    env = os.environ.get("PROTEUS_ADVBENCH_PATH")
    if env:
        return Path(env).expanduser()
    cache = Path.home() / ".cache" / "proteus" / "advbench" / "harmful_behaviors.csv"
    return download_verified(
        name="AdvBench",
        url=DATA_URL,
        expected_sha256=DATA_SHA256,
        cache=cache,
        validate=_records,
    )


def _records(path: Path) -> tuple[AdvBenchItem, ...]:
    with path.open("r", encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames is None or "goal" not in reader.fieldnames:
            raise ValueError("AdvBench CSV must include a goal column")
        items = []
        for index, row in enumerate(reader):
            goal = str(row.get("goal") or "").strip()
            target = str(row.get("target") or "").strip()
            if not goal:
                continue
            items.append(AdvBenchItem(goal=goal, target=target, index=index))
    if len(items) < SAMPLE_SIZE_MAX:
        raise ValueError("AdvBench CSV does not contain enough harmful-behavior rows")
    return tuple(items)


def load_advbench(dataset_file: str | os.PathLike | None = None) -> tuple[AdvBenchItem, ...]:
    return _records(dataset_path(dataset_file))


def sample_advbench(
    items: tuple[AdvBenchItem, ...],
    *,
    n: int = SAMPLE_SIZE,
    seed: int,
) -> tuple[AdvBenchItem, ...]:
    if n < SAMPLE_SIZE_MIN or n > SAMPLE_SIZE_MAX:
        raise ValueError("AdvBench sample size must be between 10 and 20")
    if len(items) < n:
        raise ValueError("AdvBench sample is larger than the loaded catalog")
    order = list(items)
    random.Random(seed).shuffle(order)
    return tuple(order[:n])
