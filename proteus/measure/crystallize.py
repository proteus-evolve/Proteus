"""Crystallization: does the initial condition survive removal of the disposition?

The sharpest claim in the paper. Take an evolved harness `W_t`, mount it under a neutral
disposition `F0` (remove the perturbation, keep everything the agent built), run a fresh
probe episode, and read the behaviour back. Two stages keep "the structure carries an
identity" apart from "the identity is the one we planted":

  Stage A (fidelity): under F0, is a state's probe behaviour closer to *its own* endpoint
      than to other seeds' endpoints? (structure carries an identity)
  Stage B (arm shift): is the distribution of endpoints moved by the arm, in the direction
      the installed disposition pointed? (the identity is the planted one)

This module provides the mounting + the two statistics; it drives probe episodes through
the same adapter + episode loop as evolution, so a swap is measured with the same ruler.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from proteus.core import snapshot
from proteus.core.adapter import HarnessAdapter
from proteus.core.disposition import Disposition
from proteus.measure import stream


@dataclass(frozen=True)
class Mount:
    """One cell of the swap matrix: an evolved state re-run under some disposition."""
    seed: str
    checkpoint: int          # episode whose state to mount (e.g. 10/20/30)
    disposition: Disposition # F0 (neutral) for crystallization; F_t for the deploy cell


def materialize_state(evolved_root: Path, checkpoint: int, dest: Path,
                      adapter: HarnessAdapter, disposition: Disposition) -> Path:
    """Extract W at `checkpoint` into `dest/harness` and install `disposition` on it."""
    work = evolved_root / "harness"
    sha = snapshot.commit_for_episode(work, checkpoint)
    if sha is None:
        raise ValueError(f"no snapshot for episode {checkpoint} under {evolved_root}")
    dest_harness = dest / "harness"
    snapshot.materialize(work, sha, dest_harness)
    adapter.install_disposition(dest_harness, disposition)
    return dest_harness


def fidelity(probe_stream: Sequence[str], own_endpoint: Sequence[str],
             other_endpoints: Sequence[Sequence[str]]) -> dict:
    """Stage A: distance to own endpoint vs. to others. <1 ratio = faithful to its own state.

    Zero distance to another seed's endpoint is the *worst* case for this statistic — the
    probe reproduced someone else's behaviour exactly — so the ratio must be large there.
    Returning 0.0 when `mean_other == 0` reported that case as perfect fidelity, inverting
    the conclusion; an epsilon floor keeps the ratio monotone in the direction it claims.
    """
    own = stream.freq_distance(probe_stream, own_endpoint)
    others = [stream.freq_distance(probe_stream, e) for e in other_endpoints]
    if not others:
        raise ValueError(
            "fidelity needs at least one other endpoint: with nothing to compare against, "
            "'closer to its own state than to others' is undefined")
    mean_other = sum(others) / len(others)
    return {"to_own": own, "to_others": mean_other,
            "ratio": own / max(mean_other, 1e-9), "n_others": len(others)}


def arm_shift(f0_streams: dict[str, list[list[str]]]) -> dict:
    """Stage B: is the arm's endpoint distribution moved vs. control, under F0?

    `f0_streams` maps arm label -> probe streams read back under the neutral disposition.
    Reuses the between/within statistic: if crystallized, arms still separate under F0.
    """
    return stream.between_within(f0_streams, level="freq")
