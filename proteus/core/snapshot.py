"""Per-episode git snapshots of the harness working tree.

Harness-agnostic by construction: it is plain git over whatever files the harness keeps.
Commit *t* is the harness state after episode *t*; the sequence of commits is the
evolution trajectory the measurement layer reads. A disposition installed as a patch is
removable by reverting its commit onto an evolved state — which is how crystallization
reads `W_t` back under `F0`.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


def _git(work_tree: Path, *args: str) -> str:
    git_dir = work_tree.parent / ".snapshot.git"
    return subprocess.run(
        ["git", "--git-dir", str(git_dir), "--work-tree", str(work_tree), *args],
        capture_output=True, text=True, check=True,
    ).stdout


def init(work_tree: Path) -> None:
    """Create the bare snapshot repo and commit the seeded (episode-0) state."""
    git_dir = work_tree.parent / ".snapshot.git"
    if git_dir.exists():
        return
    subprocess.run(["git", "init", "--bare", "-q", str(git_dir)], check=True)
    _git(work_tree, "config", "user.email", "proteus@localhost")
    _git(work_tree, "config", "user.name", "proteus")
    commit(work_tree, "episode 0: seeded harness")


def commit(work_tree: Path, message: str) -> str:
    """Snapshot the current working tree; returns the commit sha.

    Always creates a commit (``--allow-empty``), even when an episode changed nothing, so
    every episode maps to exactly one commit — the checkpoint mapping crystallization and
    path-length rely on must have no gaps.
    """
    _git(work_tree, "add", "-A")
    subprocess.run(
        ["git", "--git-dir", str(work_tree.parent / ".snapshot.git"),
         "--work-tree", str(work_tree), "commit", "-q", "--allow-empty", "-m", message],
        check=True,
    )
    return head(work_tree)


class SnapshotRole(str, Enum):
    ACTIVE = "active"
    CANDIDATE = "candidate"


@dataclass(frozen=True)
class SnapshotRef:
    """Public identity for a preserved logical harness snapshot."""

    run_id: str
    episode: int
    role: SnapshotRole

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "episode": self.episode,
            "role": self.role.value,
        }


def head(work_tree: Path) -> str:
    try:
        return _git(work_tree, "rev-parse", "HEAD").strip()
    except subprocess.CalledProcessError:
        return ""


def commit_for_episode(work_tree: Path, episode: int) -> str | None:
    """The sha whose message is 'episode N: ...' — used to materialise a checkpoint state."""
    git_dir = work_tree.parent / ".snapshot.git"
    log = subprocess.run(
        ["git", "--git-dir", str(git_dir), "log", "--format=%H %s"],
        capture_output=True, text=True, check=True,
    ).stdout
    for line in log.splitlines():
        sha, _, subject = line.partition(" ")
        if subject.startswith(f"episode {episode}:"):
            return sha
    return None


def freeze_candidate(
    work_tree: Path, *, run_id: str, episode: int, label: str
) -> SnapshotRef:
    """Preserve the evolved tree before any evaluator can inspect it."""
    commit(work_tree, f"candidate {episode}: {label} [run_id={run_id}]")
    return SnapshotRef(run_id=run_id, episode=episode, role=SnapshotRole.CANDIDATE)


def _candidate_record(work_tree: Path, episode: int) -> tuple[SnapshotRef, str] | None:
    git_dir = work_tree.parent / ".snapshot.git"
    log = subprocess.run(
        ["git", "--git-dir", str(git_dir), "log", "--format=%H %s"],
        capture_output=True, text=True, check=True,
    ).stdout
    prefix = f"candidate {episode}:"
    marker = " [run_id="
    for line in log.splitlines():
        sha, _, subject = line.partition(" ")
        if not subject.startswith(prefix) or not subject.endswith("]"):
            continue
        _, separator, run_id = subject.rpartition(marker)
        if separator and run_id[:-1]:
            return (
                SnapshotRef(run_id=run_id[:-1], episode=episode, role=SnapshotRole.CANDIDATE),
                sha,
            )
    return None


def candidate_for_episode(work_tree: Path, episode: int) -> SnapshotRef | None:
    """Return the logical reference for the frozen candidate, if retained."""
    record = _candidate_record(work_tree, episode)
    return record[0] if record is not None else None


def materialize_candidate(work_tree: Path, candidate: SnapshotRef, dest: Path) -> None:
    """Materialize a candidate by logical identity without exposing its Git revision."""
    record = _candidate_record(work_tree, candidate.episode)
    if record is None or record[0] != candidate:
        raise ValueError("candidate snapshot is not available for this work tree")
    materialize(work_tree, record[1], dest)


def restore(work_tree: Path, sha: str) -> None:
    """Return the working tree (and index) to the state at `sha` without moving HEAD.

    Used by accept/reject selection: a rejected episode's edits are removed and the tree
    returns to the last accepted state. Deliberately NOT `reset --hard`: that moves the
    branch pointer and orphans any commit made after `sha` — which would silently drop
    the preserved rejected-candidate commit from history. `restore --source` rewrites
    index + worktree only; `clean -fd` then removes files that became untracked (added
    after `sha`).
    """
    _git(work_tree, "restore", "--source", sha, "--staged", "--worktree", "--", ".")
    _git(work_tree, "clean", "-fd")


def materialize(work_tree: Path, sha: str, dest: Path) -> None:
    """Extract the tree at `sha` into `dest` (a fresh harness dir at a past state)."""
    git_dir = work_tree.parent / ".snapshot.git"
    dest.mkdir(parents=True, exist_ok=True)
    archive = subprocess.run(
        ["git", "--git-dir", str(git_dir), "archive", sha],
        capture_output=True, check=True,
    )
    subprocess.run(
        ["tar", "-x", "-C", str(dest)],
        input=archive.stdout,
        capture_output=True,
        check=True,
    )
