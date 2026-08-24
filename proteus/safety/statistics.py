"""Descriptive paired-block intervals that never participate in activation policy."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from enum import Enum


class DescriptiveDirection(str, Enum):
    BETTER = "better"
    WORSE = "worse"
    SAME = "same"
    INCONCLUSIVE = "inconclusive"
    INSUFFICIENT_BLOCKS = "insufficient_blocks"


@dataclass(frozen=True)
class PairedBlock:
    block_id: str
    active: float
    candidate: float

    def __post_init__(self) -> None:
        if not isinstance(self.block_id, str) or not self.block_id.strip():
            raise ValueError("paired block ID must be non-empty")
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            for value in (self.active, self.candidate)
        ):
            raise ValueError("paired block values must be finite numbers")


@dataclass(frozen=True)
class PairedDescriptiveInterval:
    estimate: float | None
    lower: float | None
    upper: float | None
    epsilon: float
    confidence: float
    blocks: int
    direction: DescriptiveDirection


def _percentile(sorted_values: list[float], probability: float) -> float:
    position = probability * (len(sorted_values) - 1)
    lower_index = math.floor(position)
    upper_index = math.ceil(position)
    if lower_index == upper_index:
        return sorted_values[lower_index]
    fraction = position - lower_index
    return (
        sorted_values[lower_index] * (1.0 - fraction)
        + sorted_values[upper_index] * fraction
    )


def paired_descriptive_interval(
    blocks: tuple[PairedBlock, ...],
    *,
    epsilon: float,
    higher_is_better: bool = True,
    confidence: float = 0.95,
    resamples: int = 2_000,
    seed: int = 0,
) -> PairedDescriptiveInterval:
    """Bootstrap independent paired blocks and classify the complete interval."""

    estimate = (
        sum(block.candidate - block.active for block in blocks) / len(blocks)
        if blocks
        else None
    )
    if len({block.block_id for block in blocks}) != len(blocks):
        raise ValueError("paired block IDs must be unique")
    if (
        isinstance(epsilon, bool)
        or not isinstance(epsilon, (int, float))
        or not math.isfinite(epsilon)
        or epsilon < 0
    ):
        raise ValueError("equivalence epsilon must be a finite non-negative number")
    if not 0.0 < confidence < 1.0:
        raise ValueError("confidence must be between zero and one")
    if isinstance(resamples, bool) or not isinstance(resamples, int) or resamples < 1:
        raise ValueError("resamples must be a positive integer")
    if len(blocks) < 2:
        return PairedDescriptiveInterval(
            estimate=estimate,
            lower=None,
            upper=None,
            epsilon=float(epsilon),
            confidence=confidence,
            blocks=len(blocks),
            direction=DescriptiveDirection.INSUFFICIENT_BLOCKS,
        )

    deltas = tuple(block.candidate - block.active for block in blocks)
    generator = random.Random(seed)
    bootstrap_means = sorted(
        sum(generator.choice(deltas) for _ in deltas) / len(deltas)
        for _ in range(resamples)
    )
    tail = (1.0 - confidence) / 2.0
    lower = _percentile(bootstrap_means, tail)
    upper = _percentile(bootstrap_means, 1.0 - tail)
    oriented_lower, oriented_upper = (lower, upper)
    if not higher_is_better:
        oriented_lower, oriented_upper = -upper, -lower
    if oriented_lower > epsilon:
        direction = DescriptiveDirection.BETTER
    elif oriented_upper < -epsilon:
        direction = DescriptiveDirection.WORSE
    elif oriented_lower >= -epsilon and oriented_upper <= epsilon:
        direction = DescriptiveDirection.SAME
    else:
        direction = DescriptiveDirection.INCONCLUSIVE
    return PairedDescriptiveInterval(
        estimate=estimate,
        lower=lower,
        upper=upper,
        epsilon=float(epsilon),
        confidence=confidence,
        blocks=len(blocks),
        direction=direction,
    )
