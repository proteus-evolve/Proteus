"""Harness-neutral contracts for gating a frozen candidate activation."""

from __future__ import annotations

import shutil
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Iterator, Protocol

from proteus.core import snapshot
from proteus.core.adapter import ActionEvent
from proteus.core.snapshot import SnapshotRef


@dataclass(frozen=True)
class CandidateGateContext:
    """Controller-only view of one frozen active/candidate transition."""

    run_id: str
    episode: int
    active: SnapshotRef
    candidate: SnapshotRef
    active_root: Path
    candidate_root: Path
    events: tuple[ActionEvent, ...]
    goal_text: str = ""


@dataclass(frozen=True)
class CandidateGateResult:
    """The gate's deliberately small activation decision."""

    allowed: bool
    status: str
    decision_ref: str


class CandidateGate(Protocol):
    def evaluate(self, context: CandidateGateContext) -> CandidateGateResult: ...


@contextmanager
def materialized_transition(
    work_tree: Path, active_commit: str, candidate: SnapshotRef
) -> Iterator[tuple[Path, Path, Path]]:
    """Yield independent active, evaluator-candidate, and gate-candidate copies.

    The evaluator copy has a sibling task directory, matching a normal run layout, so
    benchmark evaluators can keep resolving ``<run>/task`` without observing the live
    candidate tree.  The gate receives a second candidate copy so evaluator mutation
    cannot affect the controller decision.
    """
    with TemporaryDirectory(prefix="proteus-transition-") as temporary:
        root = Path(temporary)
        active_root = root / "active"
        evaluator_root = root / "evaluator"
        task_candidate_root = evaluator_root / "harness"
        gate_candidate_root = root / "gate-candidate"
        snapshot.materialize(work_tree, active_commit, active_root)
        snapshot.materialize_candidate(work_tree, candidate, task_candidate_root)
        source_task = work_tree.parent / "task"
        if source_task.exists():
            shutil.copytree(source_task, evaluator_root / "task")
        snapshot.materialize_candidate(work_tree, candidate, gate_candidate_root)
        yield active_root, task_candidate_root, gate_candidate_root


def activate_frozen_candidate(work_tree: Path, candidate: SnapshotRef, *, message: str) -> str:
    """Restore the frozen candidate and create the active episode checkpoint."""
    snapshot.restore_candidate(work_tree, candidate)
    return snapshot.commit(work_tree, message)


def reject_frozen_candidate(
    work_tree: Path, active_commit: str, candidate: SnapshotRef, *, message: str
) -> str:
    """Keep a frozen candidate discoverable while restoring the active checkpoint."""
    # Resolving first makes an unavailable logical ref a visible transaction failure
    # rather than silently treating an arbitrary working tree as the rejected candidate.
    snapshot.candidate_commit(work_tree, candidate)
    snapshot.restore(work_tree, active_commit)
    return snapshot.commit(work_tree, message)
