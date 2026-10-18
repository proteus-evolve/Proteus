"""Private integrated-verifier evidence and exactly-once service accounting."""

import json
from dataclasses import replace

import pytest

from proteus.core.contracts import OperationStatus, Outcome, RuntimeIdentity, Usage
from proteus.core.methods import MethodResult
from proteus.core.serialization import content_hash
from proteus.evaluation.records import BackendEvaluation, Feedback, Grade, SubjectResult
from proteus.evaluation.service import EvaluationSession
from test_v1_evaluation import method, plugin, register, run, setup
from test_v1_ledger import run_ledger


SECRET = "PRIVATE_BACKEND_VERIFIER_RECORD"


def integrated(tmp_path, *, hidden=False, mismatch=False, grader_mismatch=False):
    registry, store, _, recipe = setup(tmp_path, hidden=hidden)
    class Subject:
        def run(self, context):
            book = run_ledger(tmp_path / "run")
            active = [e for e in book.entries() if e["state"] == "started"]
            assert {e["identity"]["metadata"]["kind"] for e in active} == {
                "evaluation_cohort", "subject", "backend_verifier"}
            return SubjectResult(loaded_artifact_hash=context.candidate.artifact.content_hash,
                runtime=RuntimeIdentity(runtime=context.subject.runner.plugin,
                    environment_hash=content_hash(context.execution.environment)),
                status=Outcome.SUCCEEDED, output=42, state=context.state,
                usage=Usage(model_calls=2, cost_usd=0.3, wall_seconds=2),
                backend_evaluation=BackendEvaluation(producer=context.subject.runner.plugin,
                    artifact_hash=content_hash("wrong") if mismatch else context.candidate.artifact.content_hash,
                    task_hash=content_hash(context.task), trial_id="backend-" + context.task.id,
                    payload={"secret": SECRET, "reward": 1}, usage=Usage(model_calls=1, cost_usd=0.1, wall_seconds=1)))
    class Grader:
        def grade(self, task, rollout, reference, *, backend):
            assert list((tmp_path / "run").glob("evaluations/*/predictions/*.json"))
            assert backend.payload["secret"] == SECRET
            return Grade(status=Outcome.SUCCEEDED, score=1, feedback=Feedback(value=SECRET),
                usage=Usage(model_calls=99) if grader_mismatch else backend.usage)
    register(registry, "fixture.backend_subject", "subject", Subject, capabilities=("evaluation.backend.fixture",))
    register(registry, "fixture.backend_grader", "evaluator", Grader, capabilities=("evaluation.backend.fixture",))
    recipe = replace(recipe, subject=replace(recipe.subject, runner=plugin("fixture.backend_subject")),
        evaluations=(replace(recipe.evaluations[0], evaluator=plugin("fixture.backend_grader")),))
    return registry, store, recipe


@pytest.mark.parametrize("hidden", [True, False])
def test_backend_evidence_is_private_until_feedback_and_never_double_charged(tmp_path, hidden):
    registry, store, recipe = integrated(tmp_path, hidden=hidden)
    def study(services):
        session = services.open_evaluation("score")
        candidate = services.initial_candidate()
        for _ in session.batch.tasks:
            observation = session.step(candidate)
            assert observation.row is None and SECRET not in observation.to_json()
            assert SECRET not in json.dumps(services.values.export())
        receipt = session.finish()
        assert len(receipt.batches) == (0 if hidden else 1)
        return MethodResult()
    result, records, root = run(tmp_path, registry, store, method(registry, recipe, study))
    assert result.status == OperationStatus.COMPLETED, result.diagnostic
    book = run_ledger(root)
    assert len(book.entries()) == 5  # cohort + two subjects + two integrated verifiers
    assert book.balance()["charged"]["model_calls"] == 6
    assert book.balance()["charged"]["cost_usd"] == pytest.approx(0.8)
    assert book.balance()["charged"]["wall_seconds"] == 6
    assert records["results"][-1]["usage"]["model_calls"] == 6
    assert len(list(root.glob("evaluations/*/private/backend/*.json"))) == 2
    if hidden:
        assert SECRET not in json.dumps(records)
    for path in root.glob("evaluations/*/predictions/*.json"):
        assert SECRET not in path.read_text()


@pytest.mark.parametrize("mismatch,grader_mismatch", [(True, False), (False, True)])
def test_rejected_backend_or_grader_identity_keeps_actual_usage(tmp_path, mismatch, grader_mismatch):
    registry, store, recipe = integrated(tmp_path, mismatch=mismatch, grader_mismatch=grader_mismatch)
    result, records, root = run(tmp_path, registry, store, recipe)
    assert result.status == OperationStatus.COMPLETED
    assert all(row["status"] == "infra_error" and row["score"] is None
               for row in records["evaluations"][0]["rows"])
    assert run_ledger(root).balance()["charged"]["model_calls"] == 6
    assert records["results"][-1]["usage"]["model_calls"] == 6


def test_failed_private_publication_preserves_both_executed_roles(tmp_path, monkeypatch):
    registry, store, recipe = integrated(tmp_path)
    original = EvaluationSession._save
    def fail(self, name, value):
        if name.startswith("private/backend/"):
            raise OSError("injected backend publication failure")
        return original(self, name, value)
    monkeypatch.setattr(EvaluationSession, "_save", fail)
    result, records, root = run(tmp_path, registry, store, recipe)
    assert result.status == OperationStatus.FAILED
    assert records["rollouts"] == []
    assert records["results"][-1]["usage"]["model_calls"] == 3
    assert run_ledger(root).balance()["charged"]["model_calls"] == 3


def test_ordinary_grader_failure_retains_returned_usage_and_marks_violation(tmp_path):
    registry, store, _, recipe = setup(tmp_path)
    class Grader:
        def grade(self, task, rollout, reference):
            return Grade(status=Outcome.SUCCEEDED, score=1, usage=Usage(model_calls=2, cost_usd=0.5))
    register(registry, "fixture.not_deterministic", "evaluator", Grader, capabilities=("evaluator.deterministic",))
    recipe = replace(recipe, evaluations=(replace(recipe.evaluations[0], evaluator=plugin("fixture.not_deterministic")),))
    _, records, root = run(tmp_path, registry, store, recipe)
    graders = [e for e in run_ledger(root).entries() if e["identity"]["metadata"]["kind"] == "grader"]
    assert len(graders) == 2
    assert all(e["state"] == "failed" and e["settlement"]["actual"]["model_calls"] == 2
               and e["settlement"]["violations"] == ["model_calls"] for e in graders)
    assert records["evaluations"][0]["usage"]["cost_usd"] == 1
