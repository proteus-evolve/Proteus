"""ACE Formula's reference task conversion and managed deterministic grader."""

from pathlib import Path

from proteus.benchmarks.ace_formula_reference import FormulaReference, parse_context_and_question_formula
from proteus.core.contracts import Outcome, PluginRef, require
from proteus.core.plugins import EmptyOptions, Plugin, implementation_hash
from proteus.evaluation.records import Feedback, Grade, zero_usage
from proteus.methods.ace.roles import AceTask, Generation


PROCESSOR_HASH = "ba20c128d6deefb63d8246d5e300bc07a26e9bb1e0170734fb9b193eb41a4621"


def task_input(raw):
    require(isinstance(raw, dict) and set(raw) == {"context", "target"}
            and type(raw["context"]) is str and type(raw["target"]) is str,
            "Formula source row must contain string context/target")
    context, question = parse_context_and_question_formula(raw["context"])
    return AceTask(question=question, context=context)


class FormulaMetric:
    def grade(self, task, rollout, reference):
        require(type(reference) is str, "Formula reference must be a string")
        generation = Generation.from_dict(rollout.output)
        correct = FormulaReference()._formula_answer_is_correct(generation.answer, reference)
        return Grade(status=Outcome.SUCCEEDED if correct else Outcome.TASK_FAILED, score=float(correct),
            feedback=Feedback(value={"correct": correct, "ground_truth": reference}), usage=zero_usage())


def register(registry):
    root = Path(__file__).parent
    registry.register(Plugin(ref=PluginRef(name="proteus.ace_formula_metric", version="1"), kind="evaluator",
        options=EmptyOptions, factory=lambda options: FormulaMetric(), source="proteus",
        implementation_hash=implementation_hash({path.name: path for path in root.glob("ace_formula*.py")}),
        capabilities=("evaluator.deterministic",)))
