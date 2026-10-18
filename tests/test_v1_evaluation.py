"""Real candidate execution plus adversarial state, visibility and provenance cases."""

import json
import random
from dataclasses import replace
from pathlib import Path

import pytest

from proteus.artifacts.codecs import FileContent, TextComponents, TextDelta
from proteus.artifacts.store import ArtifactStore
from proteus.core.contracts import (
    BudgetLimits, Candidate, EvaluationBatch, FeedbackVisibility, OperationStatus, Outcome, PluginRef,
    Rollout, RuntimeIdentity, StageRequest, StageResult, StatePolicy, Usage, validate_records,
)
from proteus.core.methods import ActorResult, MethodResult
from proteus.core.plugins import EmptyOptions, Plugin
from proteus.core.serialization import ContractError, content_hash
from proteus.evaluation.records import Feedback, Grade, SubjectResult, zero_usage
from proteus.evaluation.service import cache_key
from proteus.evaluation.tasks import StateValue, Task, TaskDataset, TaskRecord, make_batch
from proteus.examples.candidate_evaluation import prepare, plugin
from proteus.methods.compare import CompareOptions, Variant
from proteus.recipes import load_recipe
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.config import (
    ActorSpec, EnvironmentSpec, EvaluationSpec, ExecutionSpec, RecipeConfig, ReferenceSpec, SubjectSpec, TaskSuiteSpec,
)
from proteus.recipes.manifest import SourcePin
from proteus.recipes.runner import run_recipe


SOURCE = SourcePin(name="proteus", repository="https://example.invalid/fixture", revision="0" * 40)
COUNTER = """import json, sys, os
r = json.load(sys.stdin)
n = r['state']['payload'].get('n', 0) + 1
print(json.dumps({'output': n, 'state': {'payload': {'n': n}}, 'trace': {'events': [{'pid': os.getpid()}]}}))
"""


def setup(tmp_path, *, code=COUNTER, sequence=False, timing="after_batch", hidden=False,
          references=(1, 2), variants=()):
    registry = builtin_registry()
    store = ArtifactStore(tmp_path / "input-artifacts", registry)
    initial = store.put(PluginRef(name="proteus.file_tree", version="1"), {"main.py": FileContent(code.encode())})
    dataset = TaskDataset(version="fixture-v1", records=tuple(
        TaskRecord(task=Task(id=str(i), version="1", split="train", input=i), reference=reference)
        for i, reference in enumerate(references)))
    source = tmp_path / "dataset.json"
    source.write_text(dataset.to_json())
    task_spec = TaskSuiteSpec(suite=plugin("proteus.json_tasks", path=str(source)),
        dataset_version=dataset.version, dataset_hash=content_hash(dataset), split="train",
        task_ids=tuple(str(i) for i in range(len(references))))
    options = CompareOptions(evaluations=("score",), variants=tuple(variants))
    recipe = RecipeConfig(name="evaluation-fixture", method=plugin("proteus.compare", **options.to_dict()), actors={},
        reference=ReferenceSpec(profile="native"), subject=SubjectSpec(runner=plugin("proteus.python_subject"), initial_artifact=initial),
        execution=ExecutionSpec(environment=EnvironmentSpec(backend=plugin("proteus.local_json"),
            provider="host", network="unrestricted", lifecycle="sequence" if sequence else "trial"),
            state_policy=StatePolicy.SEQUENCE if sequence else StatePolicy.INDEPENDENT, timeout_seconds=3),
        evaluations=(EvaluationSpec(id="score", evaluator=plugin("proteus.exact_match"), tasks=task_spec,
            visibility=FeedbackVisibility.HIDDEN if hidden else FeedbackVisibility.OBSERVED,
            feedback_timing="never" if hidden else timing),))
    return registry, store, dataset, recipe


def run(tmp_path, registry, store, recipe):
    root = tmp_path / "run"
    lock = registry.make_lock(recipe, [SOURCE])
    result = run_recipe(recipe, registry=registry, lock=lock, root=root, artifact_source=store.root)
    records = json.loads((root / "execution-0001.json").read_text())
    validate_records(run_id=root.name,
        candidates=[Candidate.from_dict(item) for item in records["candidates"]],
        rollouts=[Rollout.from_dict(item) for item in records["rollouts"]],
        evaluations=[EvaluationBatch.from_dict(item) for item in records["evaluations"]],
        requests=[StageRequest.from_dict(item) for item in records["requests"]],
        results=[StageResult.from_dict(item) for item in records["results"]])
    return result, records, root


def register(registry, name, kind, factory, *, capabilities=()):
    registry.register(Plugin(ref=PluginRef(name=name, version="1"), kind=kind, options=EmptyOptions,
        factory=lambda options: factory(), implementation_hash=content_hash(name), source="proteus", capabilities=capabilities))


def method(registry, recipe, body):
    class Study:
        def run(self, services, restored_state):
            return body(services)
    register(registry, "fixture.method", "method", Study)
    return replace(recipe, method=plugin("fixture.method"))


def test_executable_example_scores_actual_versions_and_keeps_test_grades_private(tmp_path):
    recipe_path = prepare(tmp_path / "example")
    recipe = load_recipe(recipe_path)
    registry = builtin_registry()
    store = ArtifactStore(recipe_path.parent / "artifact-store", registry)
    result, records, root = run(tmp_path, registry, store, recipe)
    assert result.status == OperationStatus.COMPLETED, result.diagnostic
    batches = [EvaluationBatch.from_dict(item) for item in records["evaluations"]]
    assert [(item.candidate_id, item.mean_score()) for item in batches] == [("initial", 0), ("doubled", 1)]
    assert len(records["rollouts"]) == records["reserved_evaluations"] == 6
    assert len({rollout["artifact_hash"] for rollout in records["rollouts"]}) == 2
    assert all(rollout["usage"]["model_calls"] is None for rollout in records["rollouts"])
    private = [EvaluationBatch.from_json(path.read_text()) for path in root.glob("evaluations/*/private/batches/*.json")]
    assert len(private) == 4
    assert sum(item.visibility == FeedbackVisibility.HIDDEN for item in private) == 2
    test_receipts = [item["value"] for item in records["values"] if item["ref"]["kind"] == "proteus.eval.receipt@1"
                     and not item["value"]["batches"]]
    assert len(test_receipts) == 2


@pytest.mark.parametrize("sequence, expected", [(False, [1, 1]), (True, [1, 2])])
def test_state_resets_between_candidates_and_independent_tasks(tmp_path, sequence, expected):
    registry, store, _, recipe = setup(tmp_path, sequence=sequence)
    def study(services):
        initial = services.initial_candidate()
        child = services.fork_candidate(initial)
        services.evaluate(initial, "score")
        services.evaluate(child, "score")
        return MethodResult()
    recipe = method(registry, recipe, study)
    result, records, root = run(tmp_path, registry, store, recipe)
    assert result.status == OperationStatus.COMPLETED
    assert [row["output"] for row in records["rollouts"]] == expected * 2
    assert records["rollouts"][0]["state_before"] == records["rollouts"][2]["state_before"]
    if sequence:
        assert records["rollouts"][0]["state_after"] == records["rollouts"][1]["state_before"]
    else:
        assert len({row["state_before"]["content_hash"] for row in records["rollouts"]}) == 1
    pids = []
    for path in root.glob("evaluations/*/values/*.json"):
        value = json.loads(path.read_text())
        if value["ref"]["kind"] == "proteus.eval.trace@1":
            pids.extend(event["pid"] for event in value["value"]["events"] if "pid" in event)
    assert len(pids) == len(set(pids)) == 4  # no implicit cache hit or surviving interpreter


def test_online_prediction_is_committed_before_feedback_and_candidate_state_updates(tmp_path):
    registry, store, _, recipe = setup(tmp_path, sequence=True, timing="after_prediction", references=(1, 11))
    events = []
    class Grader:
        def grade(self, task, rollout, reference):
            files = list((tmp_path / "run").glob("evaluations/*/predictions/*.json"))
            assert any(json.loads(path.read_text())["rollout"]["id"] == rollout.id for path in files)
            events.append("grade:" + task.id)
            return Grade(status=Outcome.SUCCEEDED, score=float(rollout.output == reference),
                feedback=Feedback(value={"next_state": 10}), usage=zero_usage())
    register(registry, "fixture.grader", "evaluator", Grader, capabilities=("evaluator.deterministic",))
    recipe = replace(recipe, evaluations=(replace(recipe.evaluations[0], evaluator=plugin("fixture.grader")),))
    def study(services):
        candidate = services.initial_candidate()
        session = services.open_evaluation("score")
        first = session.step(candidate)
        events.append("feedback:" + first.rollout.task_id)
        session.update_state(StateValue(payload={"n": first.feedback.value["next_state"]}))
        child = services.fork_candidate(candidate)
        second = session.step(child)
        events.append("feedback:" + second.rollout.task_id)
        receipt = session.finish()
        assert receipt.candidate_ids == (candidate.id, child.id)
        assert [batch.mean_score() for batch in receipt.batches] == [1, 1]
        return MethodResult()
    recipe = method(registry, recipe, study)
    result, records, root = run(tmp_path, registry, store, recipe)
    assert result.status == OperationStatus.COMPLETED, result.diagnostic
    assert events == ["grade:0", "feedback:0", "grade:1", "feedback:1"]
    assert [row["output"] for row in records["rollouts"]] == [1, 11]
    assert len(list(root.glob("evaluations/*/updates/*.json"))) == 1


def test_after_batch_does_not_grade_or_release_feedback_early(tmp_path):
    registry, store, _, recipe = setup(tmp_path, sequence=True)
    def study(services):
        candidate = services.initial_candidate()
        session = services.open_evaluation("score")
        for _ in session.batch.tasks:
            observation = session.step(candidate)
            assert observation.row is None and observation.feedback is None
            assert not list(session.root.glob("private/grades/*.json"))
            with pytest.raises(ContractError, match="state updates require"):
                session.update_state(StateValue(payload={"n": 99}))
        assert session.finish().batches[0].mean_score() == 1
        return MethodResult()
    result, _, _ = run(tmp_path, registry, store, method(registry, recipe, study))
    assert result.status == OperationStatus.COMPLETED, result.diagnostic


@pytest.mark.parametrize("grader_error", [False, True, "system_exit"])
def test_hidden_reference_scores_and_grader_diagnostics_never_enter_public_views(tmp_path, grader_error):
    secret = "PRIVATE_REFERENCE_NEEDLE"
    registry, store, _, recipe = setup(tmp_path, sequence=True, hidden=True, references=(secret, secret))
    if grader_error:
        class FailingGrader:
            def grade(self, task, rollout, reference):
                if grader_error == "system_exit":
                    raise SystemExit(reference)
                raise RuntimeError(reference)
        register(registry, "fixture.failure", "evaluator", FailingGrader, capabilities=("evaluator.deterministic",))
        recipe = replace(recipe, evaluations=(replace(recipe.evaluations[0], evaluator=plugin("fixture.failure")),))
    def study(services):
        candidate = services.initial_candidate()
        session = services.open_evaluation("score")
        for _ in session.batch.tasks:
            observation = session.step(candidate)
            assert observation.row is None and observation.feedback is None
        receipt = session.finish()
        assert receipt.batches == ()
        assert secret not in receipt.to_json()
        return MethodResult()
    result, records, root = run(tmp_path, registry, store, method(registry, recipe, study))
    assert result.status == OperationStatus.COMPLETED, result.diagnostic
    assert records["evaluations"] == [] and secret not in json.dumps(records)
    for path in root.glob("evaluations/**/*"):
        if path.is_file() and "private" not in path.relative_to(root).parts:
            assert secret not in path.read_text(), path
    assert any(secret in path.read_text() for path in root.glob("evaluations/*/private/grades/*.json"))


@pytest.mark.parametrize("code, expected", [
    ("raise RuntimeError('bad candidate')", Outcome.INVALID_CANDIDATE),
    ("print('not JSON')", Outcome.INVALID_CANDIDATE),
    ("import time\ntime.sleep(5)", Outcome.TIMED_OUT),
])
def test_unavailable_execution_never_becomes_valid_zero_and_sequence_stops(tmp_path, code, expected):
    registry, store, _, recipe = setup(tmp_path, code=code, sequence=True)
    if expected == Outcome.TIMED_OUT:
        recipe = replace(recipe, execution=replace(recipe.execution, timeout_seconds=0.05))
    result, records, _ = run(tmp_path, registry, store, recipe)
    assert result.status == OperationStatus.COMPLETED
    assert [row["status"] for row in records["rollouts"]] == [expected.value, "cancelled"]
    batch = EvaluationBatch.from_dict(records["evaluations"][0])
    assert batch.mean_score() is None and all(row.score is None for row in batch.rows)
    assert len(batch.rows) == 2


def test_incomplete_session_fails_instead_of_shrinking_the_cohort(tmp_path):
    registry, store, _, recipe = setup(tmp_path)
    def study(services):
        session = services.open_evaluation("score")
        session.step(services.initial_candidate())
        with pytest.raises(ContractError, match="incomplete task cohort"):
            session.finish()
        return MethodResult()
    result, records, root = run(tmp_path, registry, store, method(registry, recipe, study))
    assert result.status == OperationStatus.FAILED and "incomplete" in result.diagnostic
    assert records["evaluations"] == []
    assert len(records["rollouts"]) == 1 and len(list(root.glob("evaluations/*/incomplete.json"))) == 1
    assert records["results"][-1]["status"] == "failed"


@pytest.mark.parametrize("limit, completed", [(0, 0), (3, 2)])
def test_evaluation_budget_is_reserved_before_task_execution(tmp_path, limit, completed):
    registry, store, _, recipe = setup(tmp_path)
    def study(services):
        candidate = services.initial_candidate()
        services.evaluate(candidate, "score")
        services.evaluate(candidate, "score")
        return MethodResult()
    recipe = replace(method(registry, recipe, study), budget=BudgetLimits(max_evaluations=limit))
    result, records, _ = run(tmp_path, registry, store, recipe)
    assert result.status == OperationStatus.FAILED and "evaluation budget exhausted" in result.diagnostic
    assert len(records["rollouts"]) == records["reserved_evaluations"] == completed


def test_pinned_data_version_split_order_and_initial_state_are_verified(tmp_path):
    registry, store, dataset, recipe = setup(tmp_path)
    spec = recipe.evaluations[0].tasks
    shuffled = replace(spec, order="shuffle", seed=27, task_ids=tuple(reversed(spec.task_ids)))
    expected = list(shuffled.task_ids)
    random.Random(shuffled.seed).shuffle(expected)
    assert tuple(task.id for task in make_batch(dataset, shuffled).tasks) == tuple(expected)
    for wrong in (replace(spec, dataset_version="other"), replace(spec, split="test"),
                  replace(spec, task_ids=("unknown",)), replace(spec, dataset_hash=content_hash("changed"))):
        with pytest.raises(ContractError):
            make_batch(dataset, wrong)
    recipe = replace(recipe, execution=replace(recipe.execution, initial_state_hash=content_hash("other state")))
    result, records, _ = run(tmp_path, registry, store, recipe)
    assert result.status == OperationStatus.FAILED and "initial state differs" in result.diagnostic
    assert records["rollouts"] == []


def test_dataset_modified_after_lock_fails_before_any_subject_execution(tmp_path):
    registry, store, dataset, recipe = setup(tmp_path)
    lock = registry.make_lock(recipe, [SOURCE])
    changed = replace(dataset, records=(replace(dataset.records[0], reference="changed"), *dataset.records[1:]))
    Path(recipe.evaluations[0].tasks.suite.options["path"]).write_text(changed.to_json())
    result = run_recipe(recipe, registry=registry, lock=lock, root=tmp_path / "run", artifact_source=store.root)
    assert result.status == OperationStatus.FAILED and "content hash" in result.diagnostic
    assert not list((tmp_path / "run").glob("evaluations/*/trials"))


def test_missing_reference_is_not_silently_interpreted_as_a_null_gold_answer():
    task = Task(id="one", version="1", split="test", input="answer").to_dict()
    with pytest.raises(ContractError, match="missing"):
        TaskDataset.from_dict({"version": "1", "records": [{"task": task}]})
    dataset = TaskDataset.from_dict({"version": "1", "records": [{"task": task, "reference": None}]})
    assert dataset.records[0].reference is None  # explicitly declared JSON null is a valid reference


def test_subject_identity_mismatch_is_recorded_as_unavailable(tmp_path):
    registry, store, _, recipe = setup(tmp_path)
    class WrongSubject:
        def run(self, context):
            return SubjectResult(loaded_artifact_hash=content_hash("other candidate"),
                runtime=RuntimeIdentity(runtime=context.subject.runner.plugin), status=Outcome.SUCCEEDED,
                output=1, usage=zero_usage())
    register(registry, "fixture.wrong_subject", "subject", WrongSubject)
    recipe = replace(recipe, subject=replace(recipe.subject, runner=plugin("fixture.wrong_subject")))
    result, records, _ = run(tmp_path, registry, store, recipe)
    assert result.status == OperationStatus.COMPLETED
    assert all(row["status"] == "infra_error" for row in records["rollouts"])
    assert EvaluationBatch.from_dict(records["evaluations"][0]).mean_score() is None


def test_prompt_subject_uses_exact_candidate_and_fresh_actor_instances_with_accounting(tmp_path):
    registry, store, _, recipe = setup(tmp_path, references=("good",))
    initial = store.put(PluginRef(name="proteus.text_components", version="1"), TextComponents(components={"system.txt": "A"}))
    calls = []
    class Actor:
        model_call_bound = 1
        def __init__(self):
            self.used = False
        def execute(self, spec, request, workspace):
            assert not self.used
            self.used = True
            calls.append((spec, request, workspace))
            return ActorResult(text="good" if request.prompt.startswith("B") else "bad",
                runtime=RuntimeIdentity(runtime=spec.runtime.plugin, model=spec.model),
                usage=Usage(model_calls=1, input_tokens=3, output_tokens=1, cost_usd=0.1))
    register(registry, "fixture.actor", "actor", Actor, capabilities=("actor.execute", "session.fresh", "calls.bounded"))
    delta = TextDelta(base_hash=initial.content_hash, put={"system.txt": "B"})
    recipe = replace(recipe, subject=SubjectSpec(runner=plugin("proteus.prompt_subject"), initial_artifact=initial, actor="subject"),
        actors={"subject": ActorSpec(runtime=plugin("fixture.actor"), model="fixture-model", budget=BudgetLimits(max_model_calls=2))},
        execution=replace(recipe.execution, timeout_seconds=None), budget=BudgetLimits(max_model_calls=2, max_evaluations=2),
        method=plugin("proteus.compare", **CompareOptions(evaluations=("score",), variants=(Variant(id="revised", delta=delta.to_dict()),)).to_dict()))
    result, records, root = run(tmp_path, registry, store, recipe)
    assert result.status == OperationStatus.COMPLETED, result.diagnostic
    assert [EvaluationBatch.from_dict(item).mean_score() for item in records["evaluations"]] == [0, 1]
    assert len(calls) == 2 and calls[0][2] != calls[1][2]
    assert all(call[0].model == "fixture-model" for call in calls)
    assert sum(row["usage"]["model_calls"] for row in records["rollouts"]) == 2
    assert len(records["actor_calls"]) == 2
    from proteus.core.ledger import Ledger
    from proteus.recipes.manifest import RunManifest
    manifest = RunManifest.from_json((root / "manifest.json").read_text())
    book = Ledger(root / "private/ledger.sqlite3", run_id=manifest.run_id,
                  config_hash=manifest.config_hash, lock_hash=manifest.lock_hash)
    balance = book.balance()
    assert balance["charged"]["model_calls"] == balance["charged"]["evaluations"] == 2
    assert balance["charged"]["cost_usd"] == 0.2
    actors = [item for item in book.entries() if item["identity"]["metadata"]["kind"] == "actor"]
    assert {item["identity"]["metadata"]["trial_id"] for item in actors} == {r["id"] for r in records["rollouts"]}
    assert book.balance("role:subject")["charged"]["model_calls"] == 2


def test_cache_identity_covers_task_runtime_protocol_environment_and_sequence_history(tmp_path):
    registry, store, dataset, recipe = setup(tmp_path)
    batch = make_batch(dataset, recipe.evaluations[0].tasks)
    candidate = Candidate(id="initial", run_id="run", artifact=recipe.subject.initial_artifact,
                          created_by="op", created_attempt_id="a")
    values = dict(candidate=candidate, task=batch.tasks[0], batch=batch, subject=recipe.subject, actors=recipe.actors,
        evaluation=recipe.evaluations[0], execution=recipe.execution, state=batch.initial_state, history=(),
        position=0, seed=3, lock_hash=content_hash("lock"), implementations={"x": content_hash("code")})
    baseline = cache_key(**values)
    changes = [
        {"task": replace(values["task"], version="2")}, {"task": replace(values["task"], split="test")},
        {"task": replace(values["task"], input="changed")}, {"batch": replace(batch, dataset_hash=content_hash("data"))},
        {"actors": {"subject": ActorSpec(runtime=plugin("proteus.echo"), model_settings={"temperature": 0.7})}},
        {"evaluation": replace(recipe.evaluations[0], visibility=FeedbackVisibility.HIDDEN, feedback_timing="never")},
        {"evaluation": replace(recipe.evaluations[0], evaluator=plugin("other.evaluator"))},
        {"execution": replace(recipe.execution, seed=91)},
        {"execution": replace(recipe.execution, environment=replace(recipe.execution.environment, provider="different"))},
        {"state": StateValue(payload={"n": 3})}, {"batch": replace(batch, initial_state=StateValue(payload={"n": 1}))},
        {"history": ({"past_prediction": "changed"},)}, {"position": 1}, {"seed": 4},
        {"lock_hash": content_hash("new dependencies")}, {"implementations": {"x": content_hash("new code")}},
        {"candidate": replace(candidate, artifact=store.put(PluginRef(name="proteus.file_tree", version="1"),
            {"main.py": FileContent(b"print('other')")}))},
    ]
    assert all(cache_key(**{**values, **change}) != baseline for change in changes)
    assert cache_key(**{**values, "candidate": replace(candidate, id="same-bytes-new-lineage")}) == baseline


@pytest.mark.parametrize("status, score", [(Outcome.SCORE_MISSING, None), (Outcome.TASK_FAILED, 0.0),
                                          (Outcome.SUCCEEDED, 1.0), (Outcome.INFRA_ERROR, None)])
def test_evaluator_outcomes_keep_full_rows_and_derived_aggregate(tmp_path, status, score):
    registry, store, _, recipe = setup(tmp_path)
    class Grader:
        def grade(self, task, rollout, reference):
            return Grade(status=status, score=score, metrics={"metric": score}, usage=zero_usage())
    register(registry, "fixture.outcome", "evaluator", Grader, capabilities=("evaluator.deterministic",))
    recipe = replace(recipe, evaluations=(replace(recipe.evaluations[0], evaluator=plugin("fixture.outcome")),))
    result, records, _ = run(tmp_path, registry, store, recipe)
    assert result.status == OperationStatus.COMPLETED
    batch = EvaluationBatch.from_dict(records["evaluations"][0])
    assert len(batch.rows) == 2 and batch.mean_score() == score
    assert all(row.status == status for row in batch.rows)


def test_nonfinite_grader_score_is_unavailable_and_does_not_change_denominator(tmp_path):
    registry, store, _, recipe = setup(tmp_path)
    class Grader:
        def grade(self, task, rollout, reference):
            return Grade(status=Outcome.SUCCEEDED, score=1 if task.id == "0" else float("nan"), usage=zero_usage())
    register(registry, "fixture.nonfinite", "evaluator", Grader, capabilities=("evaluator.deterministic",))
    recipe = replace(recipe, evaluations=(replace(recipe.evaluations[0], evaluator=plugin("fixture.nonfinite")),))
    _, records, _ = run(tmp_path, registry, store, recipe)
    batch = EvaluationBatch.from_dict(records["evaluations"][0])
    assert [row.score for row in batch.rows] == [1, None]
    assert batch.mean_score() is None


@pytest.mark.parametrize("change", ["network", "resources", "worker", "dataset_pin", "model_cap", "environment_binding"])
def test_unsupported_conditions_fail_preflight_without_creating_run(tmp_path, change):
    registry, _, _, recipe = setup(tmp_path)
    if change == "network":
        recipe = replace(recipe, execution=replace(recipe.execution,
            environment=replace(recipe.execution.environment, network="none")))
    elif change == "resources":
        recipe = replace(recipe, execution=replace(recipe.execution,
            environment=replace(recipe.execution.environment, resources={"memory_gb": 1})))
    elif change == "worker":
        recipe = replace(recipe, execution=replace(recipe.execution, state_policy=StatePolicy.WORKER,
            environment=replace(recipe.execution.environment, lifecycle="worker")))
    elif change == "dataset_pin":
        recipe = replace(recipe, evaluations=(replace(recipe.evaluations[0],
            tasks=replace(recipe.evaluations[0].tasks, dataset_hash=None)),))
    elif change == "model_cap":
        recipe = replace(recipe, budget=BudgetLimits(max_model_calls=0))
    else:
        recipe = replace(recipe, subject=replace(recipe.subject, initial_artifact=replace(
            recipe.subject.initial_artifact, environment_hash=content_hash("other"))))
    with pytest.raises(ContractError):
        registry.resolve(recipe)
    assert not (tmp_path / "run").exists()


def test_changed_candidate_runtime_binding_does_not_execute(tmp_path):
    registry, store, _, recipe = setup(tmp_path)
    def study(services):
        original = services.initial_candidate()
        ref = services.artifacts.put(original.artifact.codec, services.artifacts.get(original.artifact),
            runtime=PluginRef(name="other.runtime", version="1"))
        changed = services.create_candidate(ref, parents=(original,))
        assert services.evaluate(changed, "score").batches[0].mean_score() is None
        return MethodResult()
    result, records, root = run(tmp_path, registry, store, method(registry, recipe, study))
    assert result.status == OperationStatus.COMPLETED
    assert all(row["status"] == "invalid_candidate" for row in records["rollouts"])
    assert not list(root.glob("evaluations/*/trials"))


def test_explicit_runtime_environment_and_initial_state_bindings_execute(tmp_path):
    registry, store, dataset, recipe = setup(tmp_path)
    resolved = registry.resolve(recipe)
    bound = store.put(resolved.subject.initial_artifact.codec, store.get(resolved.subject.initial_artifact),
        runtime=resolved.subject.runner.plugin, environment_hash=content_hash(resolved.execution.environment))
    recipe = replace(resolved, subject=replace(resolved.subject, initial_artifact=bound),
        execution=replace(resolved.execution, initial_state_hash=content_hash(dataset.initial_state)))
    result, records, _ = run(tmp_path, registry, store, recipe)
    assert result.status == OperationStatus.COMPLETED, result.diagnostic
    assert all(row["status"] == "succeeded" and row["artifact_hash"] == bound.content_hash for row in records["rollouts"])


def test_finished_session_and_duplicate_operation_cannot_execute_again(tmp_path):
    registry, store, _, recipe = setup(tmp_path)
    def study(services):
        candidate = services.initial_candidate()
        session = services.open_evaluation("score", operation_id="fixed")
        for _ in session.batch.tasks:
            session.step(candidate)
        session.finish()
        with pytest.raises(ContractError, match="closed"):
            session.step(candidate)
        with pytest.raises(ContractError, match="closed"):
            session.finish()
        with pytest.raises(ContractError, match="already submitted"):
            services.open_evaluation("score", operation_id="fixed")
        return MethodResult()
    result, records, _ = run(tmp_path, registry, store, method(registry, recipe, study))
    assert result.status == OperationStatus.COMPLETED
    assert len(records["rollouts"]) == records["reserved_evaluations"] == 2


def test_failed_prediction_publication_does_not_advance_state_or_allow_reexecution(tmp_path, monkeypatch):
    from proteus.evaluation.service import EvaluationSession
    registry, store, _, recipe = setup(tmp_path, sequence=True)
    original_save = EvaluationSession._save
    def save(self, name, value):
        if name.startswith("predictions/"):
            raise OSError("injected publication failure")
        return original_save(self, name, value)
    monkeypatch.setattr(EvaluationSession, "_save", save)
    def study(services):
        candidate = services.initial_candidate()
        session = services.open_evaluation("score")
        with pytest.raises(OSError, match="injected"):
            session.step(candidate)
        assert session.closed and session._state == session.batch.initial_state
        with pytest.raises(ContractError, match="closed"):
            session.step(candidate)
        return MethodResult()
    result, records, _ = run(tmp_path, registry, store, method(registry, recipe, study))
    assert result.status == OperationStatus.COMPLETED
    assert records["rollouts"] == records["evaluations"] == []
    assert records["results"][-1]["status"] == "failed"
    assert records["results"][-1]["usage"]["model_calls"] is None  # executed work was not charged as zero


def test_evaluation_rejects_cross_thread_dispatch(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    registry, store, _, recipe = setup(tmp_path)
    def study(services):
        candidate = services.initial_candidate()
        session = services.open_evaluation("score")
        with ThreadPoolExecutor(max_workers=1) as executor:
            with pytest.raises(ContractError, match="serial service"):
                executor.submit(session.step, candidate).result()
            with pytest.raises(ContractError, match="serial service"):
                executor.submit(services.open_evaluation, "score").result()
        for _ in session.batch.tasks:
            session.step(candidate)
        session.finish()
        return MethodResult()
    result, records, _ = run(tmp_path, registry, store, method(registry, recipe, study))
    assert result.status == OperationStatus.COMPLETED
    assert len(records["rollouts"]) == 2


def test_timeout_cleans_up_child_processes_holding_output_pipes(tmp_path):
    import time
    code = "import subprocess,sys,time\nsubprocess.Popen([sys.executable, '-c', 'import time; time.sleep(5)'])\ntime.sleep(5)"
    registry, store, _, recipe = setup(tmp_path, code=code, sequence=True)
    recipe = replace(recipe, execution=replace(recipe.execution, timeout_seconds=0.2))
    started = time.monotonic()
    result, records, _ = run(tmp_path, registry, store, recipe)
    assert time.monotonic() - started < 3  # killing only the parent leaves its child's pipe open for five seconds
    assert result.status == OperationStatus.COMPLETED
    assert [item["status"] for item in records["rollouts"]] == ["timed_out", "cancelled"]
