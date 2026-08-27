"""Per-episode git snapshots of the harness working tree.

Harness-agnostic by construction: it is plain git over whatever files the harness keeps.
Commit *t* is the harness state after episode *t*; the sequence of commits is the
evolution trajectory the measurement layer reads. A disposition installed as a patch is
removable by reverting its commit onto an evolved state — which is how crystallization
reads `W_t` back under `F0`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


def _git(work_tree: Path, *args: str) -> str:
    git_dir = work_tree.parent / ".snapshot.git"
    proc = subprocess.run(
        ["git", "--git-dir", str(git_dir), "--work-tree", str(work_tree), *args],
        capture_output=True, text=True, errors="replace", check=False,
    )
    if proc.returncode != 0:
        # check=True swallows stderr into an opaque exit-128 traceback; the reason is
        # the whole diagnostic (permissions, dubious ownership, an unreadable file)
        raise RuntimeError(
            f"git {args[0]} failed ({proc.returncode}) in {work_tree}: "
            f"{proc.stderr.strip()[-300:]}")
    return proc.stdout


def init(work_tree: Path) -> bool:
    """Create the bare snapshot repo and commit the seeded (episode-0) state.

    Returns False if a repo was already there (nothing was initialised) — callers that
    must start from a clean seed check this rather than assume.
    """
    git_dir = work_tree.parent / ".snapshot.git"
    if git_dir.exists():
        return False
    subprocess.run(["git", "init", "--bare", "-q", str(git_dir)], check=True)
    _git(work_tree, "config", "user.email", "proteus@localhost")
    _git(work_tree, "config", "user.name", "proteus")
    # A first commit of a large source-evolving harness can trigger detached `git gc
    # --auto`. The process then races temporary-run cleanup (and can recreate `gc.log`
    # after rmtree has visited the bare repo). Snapshots are short, append-only research
    # histories; maintenance should be explicit rather than an invisible background job.
    _git(work_tree, "config", "gc.auto", "0")
    # No ignore rules apply to a harness snapshot. The harness is the measured object, so
    # nothing in it may be invisible: not files matched by the user's global gitignore
    # (`*.jsonl` is a common one, and traces are jsonl), and not files matched by a
    # `.gitignore` the agent itself writes — that would let a harness hide its own state
    # from the instrument.
    _git(work_tree, "config", "core.excludesFile", "/dev/null")
    commit(work_tree, "episode 0: seeded harness")
    return True


def commit(work_tree: Path, message: str) -> str:
    """Snapshot the current working tree; returns the commit sha.

    Always creates a commit (``--allow-empty``), even when an episode changed nothing, so
    every episode maps to exactly one commit — the checkpoint mapping crystallization and
    path-length rely on must have no gaps.
    """
    nested = _nested_git_metadata(work_tree)
    if nested:
        names = ", ".join(str(path.relative_to(work_tree)) for path in nested[:5])
        raise RuntimeError(
            "snapshot refused nested git metadata; nested repositories become gitlinks "
            f"and cannot be restored faithfully: {names}"
        )
    # `-f`: include files any ignore rule would exclude (see `init`)
    _git(work_tree, "add", "-A", "-f", "--", ".")
    _git(work_tree, "commit", "-q", "--allow-empty", "-m", message)
    return head(work_tree)


def head(work_tree: Path) -> str:
    try:
        return _git(work_tree, "rev-parse", "HEAD").strip()
    except RuntimeError:
        return ""


def has_changes(work_tree: Path) -> bool:
    """Whether files/index differ from HEAD, including ignored and untracked files."""
    # Snapshot repos deliberately disable ignore rules for commits, but status still needs
    # --ignored to expose an interrupted candidate file matched by its own .gitignore.
    lines = _git(
        work_tree, "status", "--porcelain", "--untracked-files=all", "--ignored=matching"
    ).splitlines()
    return bool(lines)


def has_commit(work_tree: Path, sha: str) -> bool:
    """Return whether ``sha`` names a commit in this run's private snapshot repository."""
    if not sha:
        return False
    try:
        _git(work_tree, "cat-file", "-e", f"{sha}^{{commit}}")
    except RuntimeError:
        return False
    return True


def _nested_git_metadata(work_tree: Path) -> list[Path]:
    """Every nested `.git` file/dir, without descending into repository internals."""
    found: list[Path] = []
    for root, dirs, files in os.walk(work_tree):
        if ".git" in dirs:
            path = Path(root) / ".git"
            found.append(path)
            dirs.remove(".git")
        if ".git" in files:
            found.append(Path(root) / ".git")
    return sorted(found)


def _strip_nested_git_metadata(work_tree: Path) -> None:
    for path in _nested_git_metadata(work_tree):
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
        elif path.is_dir():
            shutil.rmtree(path)


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


def restore(work_tree: Path, sha: str) -> None:
    """Return the working tree (and index) to the state at `sha` without moving HEAD.

    Used by accept/reject selection: a rejected episode's edits are removed and the tree
    returns to the last accepted state. Deliberately NOT `reset --hard`: that moves the
    branch pointer and orphans any commit made after `sha` — which would silently drop
    the preserved rejected-candidate commit from history. `restore --source` rewrites
    index + worktree only; `clean -fdx` then removes files that became untracked (added
    after `sha`). The `x` matters: without it a rejected episode's ignored files survive
    the restore, and the next episode wakes up with state selection was supposed to undo.
    """
    # Nested repositories are never valid snapshot state. Removing their metadata first
    # turns them into ordinary paths so restore + clean can actually remove their files.
    _strip_nested_git_metadata(work_tree)
    _git(work_tree, "restore", "--source", sha, "--staged", "--worktree", "--", ".")
    _git(work_tree, "clean", "-fdx")


def _keep_failed_ref(work_tree: Path, candidate: str, episode: int) -> str:
    """Give a candidate commit the next durable failed-attempt ref for an episode."""
    base = f"refs/proteus/candidates/episode-{episode}-failed"
    existing = set(_git(
        work_tree, "for-each-ref", "--format=%(refname)", f"{base}*"
    ).splitlines())
    ref = base
    attempt = 2
    while ref in existing:
        ref = f"{base}-attempt-{attempt}"
        attempt += 1
    _git(work_tree, "update-ref", ref, candidate)
    return ref


def preserve_failed_candidate(work_tree: Path, restore_sha: str, episode: int,
                              message: str) -> str:
    """Keep a failed attempt under a dedicated ref, then restore the valid checkpoint.

    Unlike a scored rejection, an interrupted/infrastructure-failed episode is not a
    completed episode and must not advance the ``episode N`` mapping. Moving HEAD back
    after restoring lets resume retry the same episode, while the candidate remains
    inspectable under ``refs/proteus/candidates/episode-N-failed``. Retries never
    overwrite that first failure: later attempts use ``...-attempt-2``,
    ``...-attempt-3``, and so on.
    """
    candidate = ""
    try:
        candidate = commit(work_tree, message)
        _keep_failed_ref(work_tree, candidate, episode)
    finally:
        reset_to_checkpoint(work_tree, restore_sha)
    return candidate


def preserve_interrupted_candidate(work_tree: Path, restore_sha: str, episode: int,
                                   message: str) -> str:
    """Capture crash-time staged work, including a commit made just before the crash.

    During model phases HEAD remains at the durable checkpoint and edits are dirty. A
    machine can also die in the tiny boundary window after candidate commit but before
    rollback, where HEAD already names the candidate and status is clean. Both cases are
    retained under the normal failed-attempt refs, then the worktree and HEAD are reset.
    """
    candidate = ""
    try:
        current = head(work_tree)
        dirty = has_changes(work_tree)
        if dirty:
            candidate = commit(work_tree, message)
        elif current and current != restore_sha:
            candidate = current
        if candidate:
            _keep_failed_ref(work_tree, candidate, episode)
    finally:
        reset_to_checkpoint(work_tree, restore_sha)
    return candidate


def reset_to_checkpoint(work_tree: Path, sha: str) -> None:
    """Restore files, index, and HEAD to a known-valid checkpoint.

    This is the recovery primitive for incomplete episodes. Scored rejections deliberately
    keep their candidate in the main ancestry, but an infrastructure or snapshot failure
    must leave the run exactly resumable from the prior valid episode.
    """
    restore(work_tree, sha)
    _git(work_tree, "update-ref", "HEAD", sha)


def materialize(work_tree: Path, sha: str, dest: Path) -> None:
    """Extract the tree at `sha` into `dest` (a fresh harness dir at a past state)."""
    git_dir = work_tree.parent / ".snapshot.git"
    dest.mkdir(parents=True, exist_ok=True)
    archive = subprocess.run(
        ["git", "--git-dir", str(git_dir), "archive", sha],
        capture_output=True, check=True,
    )
    subprocess.run(["tar", "-x", "-C", str(dest)], input=archive.stdout, check=True)
