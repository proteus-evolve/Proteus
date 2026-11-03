"""Full Meta-Harness recipe: memory learning, proposal files, selection and recovery."""

from dataclasses import replace
import json
import os
import subprocess
import sys

import pytest

from proteus.core.contracts import FeedbackVisibility, OperationStatus
from proteus.core.methods import RunServices
from proteus.core.serialization import ContractError, content_hash
from proteus.examples.meta_harness_fixture import prepare, registry as make_registry
from proteus.methods.meta_harness.records import MetaOptions
from proteus.methods.meta_harness.report import report
from proteus.recipes import load_recipe
from proteus.recipes.runner import run_recipe
from test_v1_evaluation import SOURCE, plugin
from test_v1_ledger import run_ledger


def setup(root, **options):
    path = prepare(root, **options)
    return make_registry(), load_recipe(path)


def run(root, registry, recipe, *, resume=False):
    result = run_recipe(
        recipe,
        registry=registry,
        lock=registry.make_lock(recipe, [SOURCE]),
        root=root / "run",
        artifact_source=root / "artifact-store",
        resume=resume,
    )
    records = json.loads(sorted((root / "run").glob("execution-*.json"))[-1].read_text())
    return result, records


def values(records, kind):
    return [row["value"] for row in records["values"] if row["ref"]["kind"] == kind]


def summary(records):
    return values(records, "proteus.meta.summary@1")[-1]


def invocations(root):
    return sum(len(path.read_text().splitlines()) for path in root.rglob("fixture-invocations.txt"))


@pytest.mark.parametrize("mode,epochs", [("offline", 1), ("offline", 2), ("online", 1)])
def test_full_method_learns_generates_files_freezes_tests_and_resumes(tmp_path, mode, epochs):
    registry, recipe = setup(tmp_path, mode=mode, num_epochs=epochs)
    result, records = run(tmp_path, registry, recipe)
    assert result.status == OperationStatus.COMPLETED, result.diagnostic
    output = summary(records)
    assert output["finalized"] and output["selected"] == ["fewshot_all", "learned_1", "no_memory"]
    assert len(output["tests"]) == 3
    assert len(records["candidates"]) == 5
    iteration = values(records, "proteus.meta.iteration@1")[0]
    assert len(iteration["proposals"]) == 3
    assert len(iteration["diagnostics"]) == 1 and "broken_1" in iteration["diagnostics"][0]
    results = values(records, "proteus.meta.benchmark@1")
    assert len(results) == 4 and all(row["complete"] for row in results)
    assert next(row for row in results if row["system"] == "wrong_1")["correct"] == 0
    learned = next(row for row in results if row["system"] == "learned_1")
    state = json.loads(learned["memory"]["value"])
    assert len(state["rows"]) == 2  # equal validation accuracy restores the first epoch
    assert state["seen"] == (
        ["val query 0", "val query 1"] if mode == "offline" else ["train query 0", "train query 1"]
    )
    assert learned["context_chars"] == 0
    assert all(("metadata" in row) == (mode == "online") for row in state["rows"])
    assert all(row["raw_question"].startswith("raw train") for row in state["rows"])
    if mode == "offline":
        assert not any(row["task_id"].startswith("train") for row in records["rollouts"])
        assert len(learned["epoch_validations"]) == epochs
    private_tests = list((tmp_path / "run/evaluations").glob("*/private/batches/*.json"))
    assert (
        sum(json.loads(path.read_text())["visibility"] == "hidden" for path in private_tests) == 3
    )
    assert all(row["visibility"] != "hidden" for row in records["evaluations"])
    # The selected memory is frozen before the final validation; hidden test
    # starts from that exact saved state, not from its later prediction mutations.
    by_ref = {row["ref"]["id"]: row["value"] for row in records["values"]}
    test_first = next(
        row
        for row in records["rollouts"]
        if row["candidate_id"] == "learned_1" and row["task_id"] == "test-0"
    )
    protocol = json.loads(
        (
            tmp_path
            / "run/evaluations"
            / content_hash(test_first["operation_id"]).split(":")[1]
            / "protocol.json"
        ).read_text()
    )
    assert by_ref[protocol["inputs"]["memory"]["id"]] == learned["memory"]
    assert by_ref[test_first["state_before"]["id"]]["payload"] == {}
    count = invocations(tmp_path / "run")
    again, replayed = run(tmp_path, registry, recipe, resume=True)
    assert again == result and summary(replayed) == output
    assert invocations(tmp_path / "run") == count
    assert run_ledger(tmp_path / "run").balance()["active_slots"] == 0
    audited = report(tmp_path / "run", registry)
    assert len(audited["tests"]) == 3 and all(row["accuracy"] == 1 for row in audited["tests"])
    assert audited["ledger"]["charged"]["evaluations"] == len(records["rollouts"])


def test_multiple_datasets_seeds_iterations_keep_complete_frontier_and_history(tmp_path):
    registry, recipe = setup(
        tmp_path, iterations=2, seeds=(1, 2), datasets=("Symptom2Disease", "LawBench")
    )
    result, records = run(tmp_path, registry, recipe)
    assert result.status == OperationStatus.COMPLETED, result.diagnostic
    output = summary(records)
    assert len(output["benchmarks"]) == 24 and len(output["iterations"]) == 2
    assert output["selected"] == ["fewshot_all", "learned_1", "learned_2", "no_memory"]
    assert len(output["tests"]) == 16
    workspaces = list((tmp_path / "run/actors").glob("*/task/input/logs/evolution_summary.jsonl"))
    assert len(workspaces) == 2
    second = next(path for path in workspaces if "learned_1" in path.read_text())
    assert "broken_1" in second.read_text()
    assert (second.parent / "reports/iteration-1.md").is_file()
    assert all(
        row["split"] != "test"
        for path in (tmp_path / "run/actors").glob("*/task/history/view.json")
        for row in json.loads(path.read_text())["entries"]
    )


def test_training_reference_authorization_order_and_no_evaluation_charge(tmp_path):
    registry, recipe = setup(tmp_path)
    recipe = registry.resolve(recipe)
    services = RunServices(
        run_id="supervision", recipe=recipe, registry=registry, root=tmp_path / "supervision"
    )
    training = recipe.evaluations[0]
    disclosed = services.supervised_batch(training.id)
    assert [task.id for task in disclosed.batch.tasks] == ["train-0", "train-1"]
    assert [row["extras"]["raw_question"] for row in disclosed.references] == [
        "raw train 0",
        "raw train 1",
    ]
    assert (
        not services.actor_calls
        and not services.rollouts
        and services._evaluation_service.reserved == 0
    )
    for evaluation in recipe.evaluations[1:]:
        with pytest.raises(ContractError, match="authorize training reference"):
            services.supervised_batch(evaluation.id)
    with pytest.raises(ContractError, match="overlap"):
        replace(
            recipe,
            evaluations=(
                training,
                replace(recipe.evaluations[1], tasks=training.tasks),
                recipe.evaluations[2],
            ),
        )
    with pytest.raises(ContractError, match="explicitly observed optimization"):
        replace(training, purpose="validation")
    with pytest.raises(ContractError, match="explicitly observed optimization"):
        replace(training, visibility=FeedbackVisibility.HIDDEN, feedback_timing="never")
    changed = json.loads((tmp_path / "Symptom2Disease-42.json").read_text())
    changed["records"][0]["reference"]["target"] = "tampered"
    (tmp_path / "Symptom2Disease-42.json").write_text(json.dumps(changed))
    with pytest.raises(ContractError, match="hash"):
        services.supervised_batch(training.id)


def test_method_preflight_rejects_protocol_drift(tmp_path):
    registry, recipe = setup(tmp_path)
    options = MetaOptions.from_dict(recipe.method.options)
    for changed in (
        replace(recipe, reference=replace(recipe.reference, profile="reference")),
        replace(recipe, reference=replace(recipe.reference, deviations=())),
        replace(
            recipe,
            method=plugin("proteus.meta_harness", **replace(options, mode="online").to_dict()),
        ),
        replace(
            recipe,
            evaluations=tuple(
                replace(e, tasks=replace(e.tasks, order="shuffle")) for e in recipe.evaluations
            ),
        ),
        replace(recipe, stages=(replace(recipe.stages[0], failure_policy="stop"),)),
    ):
        with pytest.raises(ContractError):
            registry.resolve(changed)


DRIVER = r"""
import json, os, sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd()/'tests'))
from test_v1_meta_harness import setup, run, summary, invocations, make_registry, load_recipe
from proteus.core.recovery import Journal
root=Path(sys.argv[1]); boundary=sys.argv[2]; resume=sys.argv[3]=='resume'
if not (root/'recipe.json').exists(): registry,recipe=setup(root)
else: registry=make_registry();recipe=load_recipe(root/'recipe.json')
if not resume:
    seen={}; original=Journal.call
    def call(self,kind,*args,**kwargs):
        result=original(self,kind,*args,**kwargs)
        seen[kind]=seen.get(kind,0)+1
        if boundary==kind+':'+str(seen[kind]): os._exit(86)
        return result
    Journal.call=call
result,records=run(root,registry,recipe,resume=resume)
print(json.dumps({'status':result.status.value,'diagnostic':result.diagnostic,
                 'summary':summary(records) if result.status.value=='completed' else None,
                 'calls':invocations(root/'run')}))
"""


@pytest.mark.parametrize(
    "boundary",
    [
        "evaluation.supervision:1",
        "code.call:2",
        "stage:1",
        "meta.freeze:1",
        "evaluation.finish:9",
        "method.result:1",
    ],
)
def test_hard_exit_replays_settled_work_without_reissuing_calls(tmp_path, boundary):
    def invoke(resume):
        return subprocess.run(
            [sys.executable, "-c", DRIVER, str(tmp_path), boundary, "resume" if resume else "run"],
            capture_output=True,
            text=True,
            timeout=90,
        )

    stopped = invoke(False)
    assert stopped.returncode == 86, stopped.stdout + stopped.stderr
    resumed = invoke(True)
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    output = json.loads(resumed.stdout)
    assert output["status"] == "completed", output
    book = run_ledger(tmp_path / "run")
    before = book.events()
    assert book.balance()["active_slots"] == 0
    repeated = invoke(True)
    assert repeated.returncode == 0, repeated.stdout + repeated.stderr
    assert json.loads(repeated.stdout) == output and book.events() == before


def test_all_pinned_domain_files_prepare_without_inference():
    source = os.environ.get("PROTEUS_META_HARNESS_SOURCE")
    if not source:
        pytest.skip("pinned Meta-Harness source required in CI")
    from proteus.methods.meta_harness.data import DIRECTORIES, SIZES, dataset

    for name in DIRECTORIES:
        data, orders, provenance = dataset(source, name, 42)
        assert [len(orders[split]) for split in ("train", "val", "test")] == list(SIZES[name])
        assert len(provenance) == 3
        assert sum(len(v) for v in orders.values()) == len(data.records)


@pytest.mark.parametrize("damage", ["wrong_grade", "missing_cohort", "changed_learning"])
def test_report_rejects_inconsistent_private_results_and_learning_evidence(tmp_path, damage):
    registry, recipe = setup(tmp_path, iterations=0)
    result, records = run(tmp_path, registry, recipe)
    assert result.status == OperationStatus.COMPLETED, result.diagnostic
    assert report(tmp_path / "run", registry)["dataset_profile"].startswith("custom/fixture")
    if damage == "changed_learning":
        import sqlite3

        book = run_ledger(tmp_path / "run")
        entry = next(
            row
            for row in book.entries()
            if row["identity"]["metadata"]["kind"] == "code_invocation"
            and row["settlement"]["result"]["code_result"]["state"]["initialized"]
        )
        changed = entry["settlement"]
        changed["result"]["code_result"]["call_hash"] = content_hash("wrong call")
        with sqlite3.connect(tmp_path / "run/private/ledger.sqlite3") as db:
            db.execute(
                "UPDATE entries SET settlement_json=? WHERE id=?",
                (json.dumps(changed), entry["id"]),
            )
    else:
        path = next(
            path
            for path in (tmp_path / "run/evaluations").glob("*/private/batches/*.json")
            if json.loads(path.read_text())["visibility"] == "hidden"
        )
        if damage == "missing_cohort":
            path.unlink()
        else:
            batch = json.loads(path.read_text())
            batch["rows"][0]["score"] = 0
            batch["rows"][0]["status"] = "task_failed"
            path.chmod(0o644)
            path.write_text(json.dumps(batch))
    with pytest.raises(ContractError):
        report(tmp_path / "run", registry)


def test_unavailable_hidden_grades_cannot_pass_the_private_audit(tmp_path, monkeypatch):
    from proteus.methods.meta_harness.grading import MetaGrade

    original = MetaGrade.grade

    def grade(self, task, *args):
        if task.split == "test":
            raise RuntimeError("fixture grader unavailable")
        return original(self, task, *args)

    monkeypatch.setattr(MetaGrade, "grade", grade)
    registry, recipe = setup(tmp_path, iterations=0)
    result, _ = run(tmp_path, registry, recipe)
    assert result.status == OperationStatus.COMPLETED
    with pytest.raises(ContractError, match="unavailable scores"):
        report(tmp_path / "run", registry)


@pytest.mark.skipif(
    not os.environ.get("PROTEUS_HARBOR_PYTHON"), reason="requires pinned Harbor and real Docker"
)
def test_live_harbor_meta_recipe_candidates_learning_selection_and_tests(tmp_path):
    from pathlib import Path
    from proteus.core.contracts import StatePolicy
    from proteus.core.serialization import canonical_json
    from proteus.examples.harbor_evaluation import IMAGE
    from proteus.execution.harbor import identify_runtime
    from proteus.recipes.config import ExecutionSpec, EnvironmentSpec
    from test_v1_harbor_code import evidence

    root = Path(os.environ.get("PROTEUS_HARBOR_STUDY", str(tmp_path))) / "meta-harness-recipe"
    python = os.environ["PROTEUS_HARBOR_PYTHON"]
    runtime = identify_runtime(python)
    execution = ExecutionSpec(
        environment=EnvironmentSpec(
            backend=plugin(
                "proteus.harbor_code", python=python, runtime_hash=runtime["runtime_hash"]
            ),
            provider="docker",
            image_digest=IMAGE,
            resources={"cpus": 1, "memory_mb": 512},
            network="none",
            lifecycle="sequence",
        ),
        state_policy=StatePolicy.SEQUENCE,
        timeout_seconds=90,
    )
    registry, recipe = setup(root, execution=execution, task_count=1)
    result, records = run(root, registry, recipe)
    assert result.status == OperationStatus.COMPLETED, result.diagnostic
    audited = report(root / "run", registry)
    assert len(audited["tests"]) == 3 and all(row["accuracy"] == 1 for row in audited["tests"])
    assert len(records["candidates"]) == 5 and len(records["rollouts"]) == 11
    paths = evidence(root / "run", 20)
    assert (
        len(
            {
                json.loads((path.parent / "loaded.json").read_text())["container_id"]
                for path in paths
            }
        )
        == 20
    )
    (root / "audited-report.json").write_text(canonical_json(audited) + "\n")
    assert run(root, registry, recipe, resume=True)[0] == result
    evidence(root / "run", 20)
