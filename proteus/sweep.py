"""Run a grid of self-evolution trajectories — the paper's Step-2 experiment.

A sweep is {arm (disposition)} × {seed}, each a full N-episode trajectory under one
`GoalConfig`. Every run root is kept (the harness is the dependent variable). This is the
harness-agnostic analogue of the research grid: the same sweep runs the minimal harness,
Aki, or any plugged-in adapter, under no-goal or goal.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from proteus.core.activation import CandidateGate
from proteus.core.adapter import HarnessAdapter
from proteus.core.disposition import Disposition
from proteus.core.episode import RunConfig, run
from proteus.core.goal import GoalConfig


@dataclass
class SweepConfig:
    name: str
    adapter_factory: Callable[[], HarnessAdapter]
    arms: Sequence[Disposition]
    seeds: int
    goal: GoalConfig
    root: Path
    model: str = "mock"
    episodes: int = 30
    max_turns: int = 100
    candidate_gate_factory: Callable[[str], CandidateGate] | None = None


def opaque_id(arm: str, seed: int) -> str:
    """Opaque run-dir name: the subject reads its own path, so it must not spell the arm."""
    import hashlib
    return "run-" + hashlib.sha1(f"{arm}:{seed}".encode()).hexdigest()[:12]


def run_sweep(cfg: SweepConfig) -> list[dict]:
    cfg.root.mkdir(parents=True, exist_ok=True)

    # manifest first: the live report discovers every planned run from it, so tracking
    # works from episode 1 — not only after a seed completes
    runs = [{"id": opaque_id(arm.label, s), "arm": arm.label, "seed": s}
            for arm in cfg.arms for s in range(cfg.seeds)]
    (cfg.root / "manifest.json").write_text(json.dumps({
        "name": cfg.name, "episodes": cfg.episodes,
        "arms": [a.label for a in cfg.arms], "seeds": cfg.seeds, "runs": runs,
    }, indent=1))

    records: list[dict] = []
    records_path = cfg.root / "seeds.jsonl"
    with records_path.open("a", encoding="utf-8") as sink:
        for arm in cfg.arms:
            for s in range(cfg.seeds):
                rid = opaque_id(arm.label, s)
                run_root = cfg.root / "runs" / rid
                rc = RunConfig(
                    name=arm.label, run_id=rid, adapter=cfg.adapter_factory(), disposition=arm,
                    goal=cfg.goal, root=run_root, model=cfg.model,
                    episodes=cfg.episodes, max_turns=cfg.max_turns, seed=s,
                    progress_path=cfg.root / "progress" / f"{rid}.jsonl",
                    candidate_gate=(cfg.candidate_gate_factory(rid)
                                    if cfg.candidate_gate_factory is not None else None),
                )
                res = run(rc)
                rec = {"arm": arm.label, "seed": s, "root": str(run_root),
                       "episodes_complete": res.episodes_complete, "error": res.error}
                records.append(rec)
                sink.write(json.dumps(rec) + "\n")
                sink.flush()
    return records
