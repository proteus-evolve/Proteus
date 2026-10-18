"""Pinned MCE scoring with explicit, delayed training feedback."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from proteus.core.contracts import Outcome, require
from proteus.core.serialization import Contract
from proteus.evaluation.records import Feedback, Grade, zero_usage
from proteus.methods.meta_harness.reference.data.evaluators import get_evaluator


@dataclass(frozen=True, kw_only=True)
class MetaGradeOptions(Contract):
    dataset: Literal["USPTO", "Symptom2Disease", "LawBench"]


class MetaGrade:
    def __init__(self, options):
        self.options = options

    def grade(self, task, rollout, reference):
        if rollout.status != Outcome.SUCCEEDED or type(rollout.output) is not str:
            return Grade(
                status=Outcome.INVALID_CANDIDATE,
                score=None,
                usage=zero_usage(),
                diagnostic="memory prediction must succeed and return text",
            )
        require(
            isinstance(reference, Mapping) and set(reference) == {"target", "extras"},
            "invalid MCE reference",
        )
        extras = reference["extras"]
        kwargs = {
            **extras,
            **({"input_nums": extras["raw_input"]} if "raw_input" in extras else {}),
        }
        raw = get_evaluator(self.options.dataset)(rollout.output, reference["target"], **kwargs)
        correct, metrics = (
            (raw["was_correct"], raw.get("metrics", {}))
            if isinstance(raw, dict)
            else (bool(raw), {})
        )
        return Grade(
            status=Outcome.SUCCEEDED if correct else Outcome.TASK_FAILED,
            score=float(correct),
            metrics=metrics,
            feedback=Feedback(
                value={
                    "ground_truth": reference["target"],
                    "correct": correct,
                    "metrics": metrics,
                    "extras": extras,
                }
            ),
            usage=zero_usage(),
        )
