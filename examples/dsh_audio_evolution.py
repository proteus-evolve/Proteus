"""Run the public DeepSeek Harness audio-modality evolution experiment.

Build the pinned rc.8 source image first (docs/DSH_AUDIO_EVOLUTION.md), export a DeepSeek
API key, then run:

    python examples/dsh_audio_evolution.py --out runs/dsh-audio-live

The equivalent CLI is printed by ``--dry-run``.  The Python entry point exists so the
campaign's exact goal and benchmark stay versioned rather than copied from a shell history.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from proteus.adapters.dsh import DshHarness
from proteus.bench.dsh_audio import GOAL_TEXT, NAME, evaluate_audio_capability
from proteus.core import EvaluatorSpec, GoalConfig, NEUTRAL, Visibility
from proteus.report import write_report
from proteus.sweep import SweepConfig, run_sweep


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="runs/dsh-audio-live")
    parser.add_argument("--episodes", type=int, default=12)
    parser.add_argument("--max-turns", type=int, default=100)
    parser.add_argument("--model", default="deepseek-v4-flash")
    parser.add_argument("--image", default="proteus-env-dsh-src:0.1.0-rc.8")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()

    benchmark = EvaluatorSpec(
        name=NAME,
        run=evaluate_audio_capability,
        kind="benchmark",
        visibility=Visibility.OBSERVE,
    )
    root = Path(args.out).expanduser().resolve()
    cfg = SweepConfig(
        name="dsh-rc8-audio-modality",
        adapter_factory=lambda: DshHarness(image=args.image),
        arms=(NEUTRAL,),
        seeds=1,
        goal=GoalConfig.of(text=GOAL_TEXT, evaluators=(benchmark,)),
        root=root,
        model=args.model,
        episodes=args.episodes,
        max_turns=args.max_turns,
        min_turns_per_phase=5,
        announce_budget=True,
        on_existing="resume" if args.resume else "refuse",
    )
    root.mkdir(parents=True, exist_ok=True)
    write_report(root)
    print(f"Live local report: {root / 'report.html'}", flush=True)
    run_sweep(cfg)
    write_report(root)


if __name__ == "__main__":
    main()
