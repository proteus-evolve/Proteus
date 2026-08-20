"""Run a grid of self-evolution trajectories — the paper's Step-2 experiment.

A sweep is {arm (disposition)} × {seed}, each a full N-episode trajectory under one
`GoalConfig`. Every run root is kept (the harness is the dependent variable). This is the
harness-agnostic analogue of the research grid: the same sweep runs the minimal harness,
Aki, or any plugged-in adapter, under no-goal or goal.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from proteus.core.adapter import HarnessAdapter
from proteus.core.disposition import Disposition
from proteus.core.episode import RunConfig, completed_episodes, run
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
    task: object | None = None
    """A `BenchTask` to seed into every run (set automatically when a benchmark
    evaluator is attached)."""
    min_turns_per_phase: int = 0
    announce_budget: bool = False
    on_existing: str = "refuse"
    """What to do when a run root is already there: "refuse" (default), "resume", or
    "overwrite".

    Run ids are deterministic in (arm, seed), so a second sweep into the same root lands
    on the same directories. Left alone, that silently continues each seed from the
    previous sweep's *evolved* harness rather than from a clean seed, appends a second
    record per seed, and writes a second "episode 1" into the same snapshot history —
    contamination that is invisible in the output. "resume" skips seeds already recorded
    complete and picks a partial one up at the episode after its last snapshot; "overwrite"
    discards the old run roots."""


def opaque_id(arm: str, seed: int) -> str:
    """Opaque run-dir name: the subject reads its own path, so it must not spell the arm."""
    import hashlib
    return "run-" + hashlib.sha1(f"{arm}:{seed}".encode()).hexdigest()[:12]


def completed_seeds(root: Path, episodes: int) -> set[tuple[str, int]]:
    """(arm, seed) pairs recorded as having finished all `episodes`, from seeds.jsonl."""
    done: set[tuple[str, int]] = set()
    path = root / "seeds.jsonl"
    if not path.exists():
        return done
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("episodes_complete", 0) >= episodes and not row.get("error"):
            done.add((row["arm"], row["seed"]))
    return done


def completed_episodes_in(run_root: Path) -> int:
    """Episodes already snapshotted in a run root, without needing its RunConfig."""
    from types import SimpleNamespace
    return completed_episodes(SimpleNamespace(root=run_root))


def run_sweep(cfg: SweepConfig) -> list[dict]:
    if cfg.on_existing not in ("refuse", "resume", "overwrite"):
        raise ValueError(f"on_existing must be refuse/resume/overwrite, got {cfg.on_existing!r}")
    cfg.root.mkdir(parents=True, exist_ok=True)
    if cfg.on_existing == "overwrite":
        # discarding the run roots while appending to their records would leave two
        # seed rows and two episode-1 progress lines per run — records go with the runs
        (cfg.root / "seeds.jsonl").unlink(missing_ok=True)
        shutil.rmtree(cfg.root / "progress", ignore_errors=True)
    done = completed_seeds(cfg.root, cfg.episodes) if cfg.on_existing == "resume" else set()

    # manifest first: the live report discovers every planned run from it, so tracking
    # works from episode 1 — not only after a seed completes
    runs = [{"id": opaque_id(arm.label, s), "arm": arm.label, "seed": s}
            for arm in cfg.arms for s in range(cfg.seeds)]
    (cfg.root / "manifest.json").write_text(json.dumps({
        "name": cfg.name, "episodes": cfg.episodes,
        "arms": [a.label for a in cfg.arms], "seeds": cfg.seeds, "runs": runs,
        "goal": cfg.goal.goal_text(),
        "model": cfg.model,
        "evaluators": cfg.goal.describe(),
        "announce_budget": cfg.announce_budget,
    }, indent=1))

    records: list[dict] = []
    records_path = cfg.root / "seeds.jsonl"
    with records_path.open("a", encoding="utf-8") as sink:
        for arm in cfg.arms:
            for s in range(cfg.seeds):
                rid = opaque_id(arm.label, s)
                run_root = cfg.root / "runs" / rid
                start = 0
                if run_root.exists():
                    if cfg.on_existing == "refuse":
                        raise FileExistsError(
                            f"{run_root} already holds a run of arm {arm.label!r} seed {s}. "
                            "Sweeping into it again would continue that seed's evolved "
                            "harness instead of starting clean. Use a fresh --out, or "
                            "on_existing='resume' / 'overwrite'.")
                    if (arm.label, s) in done:
                        continue
                    if cfg.on_existing == "overwrite":
                        shutil.rmtree(run_root)
                    else:
                        # resume: pick the seed up where it stopped rather than paying for
                        # its finished episodes twice
                        start = completed_episodes_in(run_root)
                        if start:
                            print(f"resuming {arm.label} seed {s} at episode {start + 1}",
                                  flush=True)
                rc = RunConfig(
                    name=arm.label, adapter=cfg.adapter_factory(), disposition=arm,
                    goal=cfg.goal, root=run_root, model=cfg.model,
                    episodes=cfg.episodes, max_turns=cfg.max_turns, seed=s,
                    min_turns_per_phase=cfg.min_turns_per_phase,
                    announce_budget=cfg.announce_budget, task=cfg.task,
                    progress_path=cfg.root / "progress" / f"{rid}.jsonl",
                )
                res = run(rc, start=start)
                rec = {"arm": arm.label, "seed": s, "root": str(run_root),
                       "episodes_complete": res.episodes_complete, "error": res.error,
                       "counters": res.counters}
                records.append(rec)
                sink.write(json.dumps(rec) + "\n")
                sink.flush()
    return records
