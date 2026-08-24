"""No-goal self-evolution under three action preferences, measured with one ruler.

    python examples/no_goal_vs_review.py

Runs the offline `minimal` harness under neutral / review-notes / review-tools with no goal,
then prints the structural (what got built) and behavioural (R) measurements. This is the
shape of the paper's Step-2 experiment, in miniature and dependency-free.
"""

import statistics as st
import tempfile
from pathlib import Path

from proteus.adapters.minimal import MinimalHarness
from proteus.core import NEUTRAL, GoalConfig, review
from proteus.core.episode import RunConfig, run
from proteus.measure import distance, stream

ARMS = [NEUTRAL, review("notes"), review("tools")]
SEEDS, EPISODES = 5, 10


def main() -> None:
    root = Path(tempfile.mkdtemp(prefix="proteus_"))
    surfaces = MinimalHarness().surfaces()
    arm_units, arm_streams = {}, {}
    for arm in ARMS:
        arm_units[arm.label], arm_streams[arm.label] = [], []
        for s in range(SEEDS):
            cfg = RunConfig(name=arm.label, run_id=f"{arm.label}-{s}", adapter=MinimalHarness(), disposition=arm,
                            goal=GoalConfig.no_goal(), root=root / arm.label / str(s),
                            model="mock", episodes=EPISODES, seed=s)
            res = run(cfg)
            u = distance.units(Path(res.root) / "harness", surfaces)
            arm_units[arm.label].append({k: len(v) for k, v in u.items()})
            trace = MinimalHarness().read_trace(Path(res.root), res.episodes_complete)
            arm_streams[arm.label].append(stream.tool_stream(trace))

    print(f"{'arm':<16}{'notes':>8}{'tools':>8}   (mean units built over {SEEDS} seeds)")
    for arm in ARMS:
        us = arm_units[arm.label]
        print(f"{arm.label:<16}{st.mean(u['notes'] for u in us):>8.1f}"
              f"{st.mean(u['tools'] for u in us):>8.1f}")
    r = stream.between_within(arm_streams, level="freq", permutations=2000)
    print(f"\nbehavioural R (between/within arms): {r['R']:.3f}  p={r['p']:.4f}")
    print(f"runs kept under: {root}")


if __name__ == "__main__":
    main()
