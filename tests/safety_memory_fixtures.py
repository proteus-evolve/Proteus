"""Synthetic AdvBench rows for offline memory-family tests."""

from __future__ import annotations

from proteus.safety.advbench import AdvBenchItem


def synthetic_advbench(n: int = 12) -> tuple[AdvBenchItem, ...]:
    return tuple(
        AdvBenchItem(
            goal=f"Repeat the marker TOKEN_{index:02d} in a new note",
            target=f"Sure, here is TOKEN_{index:02d}",
            index=index,
        )
        for index in range(n)
    )
