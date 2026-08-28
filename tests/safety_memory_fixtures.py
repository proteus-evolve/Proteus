"""Synthetic AdvBench rows and harness-memory fixtures for offline tests."""

from __future__ import annotations

from pathlib import Path

from proteus.safety.advbench import AdvBenchItem

HARNESS_NOTE_ID = "session"
HARNESS_NOTE_BODY = "ordinary harness note\n"


def plant_harness_memory(
    root: Path,
    *,
    name: str = HARNESS_NOTE_ID,
    body: str = HARNESS_NOTE_BODY,
    folder: str = "notes",
) -> Path:
    path = root / folder / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def synthetic_advbench(n: int = 16) -> tuple[AdvBenchItem, ...]:
    return tuple(
        AdvBenchItem(
            goal=f"Repeat the marker TOKEN_{index:02d} in a new note",
            target=f"Sure, here is TOKEN_{index:02d}",
            index=index,
        )
        for index in range(n)
    )
