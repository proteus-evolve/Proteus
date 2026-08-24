"""Narrow, harness-neutral candidate activation boundary."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Protocol

from proteus.core import snapshot
from proteus.core.adapter import ActionEvent
from proteus.core.snapshot import SnapshotRef


@dataclass(frozen=True)
class CandidateGateContext:
    run_id: str
    episode: int
    active: SnapshotRef
    candidate: SnapshotRef
    active_root: Path
    candidate_root: Path
    adapter_name: str
    events: tuple[ActionEvent, ...]


@dataclass(frozen=True)
class CandidateGateResult:
    allowed: bool
    status: str
    decision_ref: str


class CandidateGate(Protocol):
    def evaluate(self, context: CandidateGateContext) -> CandidateGateResult: ...


@contextmanager
def materialized_transition(
    work_tree: Path, active_commit: str, candidate: SnapshotRef
) -> Iterator[tuple[Path, Path]]:
    """Provide isolated active/candidate trees to evaluators for one transition."""
    with TemporaryDirectory(prefix="proteus-transition-") as temporary:
        root = Path(temporary)
        active_root = root / "active"
        candidate_root = root / "candidate"
        snapshot.materialize(work_tree, active_commit, active_root)
        snapshot.materialize_candidate(work_tree, candidate, candidate_root)
        yield active_root, candidate_root
