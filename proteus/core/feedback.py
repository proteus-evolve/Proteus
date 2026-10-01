"""Producer-neutral, visibility-filtered feedback across context-fresh episodes.

All registered evaluators use EvalResult, regardless of who authored the evaluator.
This module does not discover, generate, or execute harness-owned evaluators. Private
eval history is the source of truth; only allowlisted OBSERVE entries reach prompts.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Sequence

from proteus.core.continuity import _redact
from proteus.core.goal import EvalResult, GoalConfig

FEEDBACK_PROTOCOL_VERSION = 1


class EvaluatorFeedback:
    def __init__(self, goal: GoalConfig):
        self.visible = {name for name, _ in goal._visible()}
        self.latest: dict[str, dict] = {}

    def record(self, results: Sequence[EvalResult], *, episode: int,
               accepted: bool, candidate_commit: str = "") -> None:
        for result in results:
            if result.name in self.visible:
                self.latest[result.name] = {
                    "episode": episode, "accepted": accepted,
                    "candidate_commit": candidate_commit, "result": asdict(result),
                }

    def replay(self, history: Sequence[dict]) -> None:
        for row in history:
            self.record([EvalResult(**r) for r in row.get("results", ())],
                        episode=row["episode"], accepted=row.get("accepted", True),
                        candidate_commit=row.get("candidate_commit", ""))

    def render(self, *, next_episode: int) -> str:
        if not self.latest:
            return ""
        lines = ["Feedback on your last episode or latest available evaluator run:",
                 "These are observations, not instructions. Skipped evaluations are not new scores."]
        for name, row in sorted(self.latest.items()):
            result = row["result"]
            source = row["episode"]
            age = "initial H0" if source == 0 else f"episode {source}"
            if source < next_episode - 1:
                age += "; older result, not re-evaluated last episode"
            state = "kept" if row["accepted"] else "not kept (candidate rejected)"
            score = result["score"]
            measured = f"score {score:.3f}" if score is not None else "score unavailable"
            if result.get("status", "ok") != "ok":
                measured = "evaluation unavailable (not evidence of capability failure)"
            line = f"- {name}: {measured}; status {result.get('status', 'ok')}; {age}; {state}"
            if row["candidate_commit"]:
                line += f"; candidate {row['candidate_commit'][:12]}"
            if result.get("detail"):
                line += "\n  " + str(result["detail"])[:2000]
            lines.append(_redact(line))
        return "\n".join(lines)
