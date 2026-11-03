"""Complete Formula comparison arms over real local HTTP transport and audit."""

from dataclasses import replace
import json
import os
import subprocess
import sys

import pytest

from proteus.core.serialization import ContractError, content_hash
from proteus.evaluation import study_timing
from proteus.evaluation.tasks import Task, TaskDataset, TaskRecord
from proteus.examples.combee_formula import Settings, execute, load, prepare, report
from proteus.examples.candidate_evaluation import plugin
from proteus.operators.combee.batching import DynamicBatchOptions
from proteus.recipes.config import ActorSpec
from test_v1_ace_formula import endpoint as formula_endpoint
from test_v1_evaluation import SOURCE
from test_v1_ledger import run_ledger

endpoint = formula_endpoint


def setup(root, endpoint, *, modes=("offline", "online"), variants=None, train_count=9):
    dataset = TaskDataset(version="combee-formula-http-fixture-1", records=tuple(
        TaskRecord(task=Task(id=f"{split}-{index}", version="1", split=split,
            input={"question": f"Formula fixture {split}-{index}", "context": ""}),
            reference="1.000000000000000001" if split == "test" else "1")
        for split, count in (("train", train_count), ("validation", 2), ("test", 5)) for index in range(count)))
    actors = {role: ActorSpec(runtime=plugin("proteus.chat", base_url=endpoint[0], key_env="FORMULA_FIXTURE_KEY"),
                             model=f"fixture-{role}", model_settings={"temperature": 0, "max_tokens": 4096})
              for role in ("generator", "reflector", "curator")}
    settings = Settings(**actors, seeds=(0,), modes=modes, eval_steps=3, online_eval_frequency=3,
        batch_size=3, max_workers=3, dynamic_batch=DynamicBatchOptions(max_batch_size=4, profile_batch_sizes=(1, 2, 3)))
    if variants is not None:
        settings = replace(settings, variants=variants)
    prepare(root, dataset, settings, sources=[SOURCE])
    return dataset, settings


def test_all_arms_share_protocol_and_budgets_with_complete_private_results(tmp_path, endpoint):
    root = tmp_path / "study"
    _, settings = setup(root, endpoint)
    study, _, _, runs = load(root)
    for mode in settings.modes:
        recipes = [recipe for entry, recipe, _ in runs if entry.mode == mode]
        assert len(recipes) == 4
        assert len({content_hash(recipe.actors) for recipe in recipes}) == 1
        assert len({content_hash(recipe.budget) for recipe in recipes}) == 1
        assert len({content_hash(recipe.evaluations) for recipe in recipes}) == 1
        assert len({recipe.subject.initial_artifact.content_hash for recipe in recipes}) == 1
    result = execute(root)
    assert result["dataset_profile"] == "custom/fixture; not Formula reproduction"
    assert len(result["runs"]) == 8 and len(result["aggregate"]) == 8
    for row in result["runs"]:
        assert row["samples"] == (9 if row["mode"] == "offline" else 5)
        assert row["baseline_accuracy"] == 0
        assert row["elapsed"]["wall_seconds"] > 0 and not row["elapsed"]["interrupted"]
        assert row["ledger"]["charged"]["cost_usd"] is None
        calls = row["ledger"]["charged"]["model_calls"]
        assert row["tokens"] == {"input_tokens": 5 * calls, "output_tokens": 3 * calls}
        assert sum(row["role_calls"].values()) == calls
        if row["variant"] == "naive_parallel":
            assert row["role_calls"]["curator"] == len(row["batch_sizes"])
        if row["variant"] == "combee_dynamic":
            assert row["batch_controller"]["selected_size"] is not None
    assert all(value["cost_usd"]["mean"] is None for value in result["aggregate"].values())
    total = len(endpoint[1])
    assert sum(row["ledger"]["charged"]["model_calls"] for row in result["runs"]) == total
    assert execute(root, resume=True) == result and report(root) == result and len(endpoint[1]) == total
    assert study == load(root)[0]
    with pytest.raises(ContractError, match="use resume"):
        execute(root)


def test_study_timing_preserves_completed_measurement_and_marks_interrupted_interval_unknown(tmp_path):
    identity = (content_hash("config"), content_hash("lock"))
    calls = []
    elapsed = study_timing.measure(tmp_path / "complete", *identity, lambda: calls.append("normal"))
    assert elapsed.wall_seconds is not None
    assert study_timing.measure(tmp_path / "complete", *identity, lambda: calls.append("resume")) == elapsed
    assert calls == ["normal", "resume"]

    def interrupted():
        raise RuntimeError("interrupted run")

    with pytest.raises(RuntimeError):
        study_timing.measure(tmp_path / "interrupted", *identity, interrupted)
    resumed = study_timing.measure(tmp_path / "interrupted", *identity, lambda: None)
    assert resumed.interrupted and resumed.wall_seconds is None
    with pytest.raises(ContractError, match="identity"):
        study_timing.read(tmp_path / "complete", content_hash("different"), identity[1])


def test_prepared_study_rejects_configuration_drift_before_any_calls(tmp_path, endpoint):
    root = tmp_path / "study"
    setup(root, endpoint, modes=("offline",), variants=("naive_parallel",))
    path = root / "study.json"
    raw = json.loads(path.read_text())
    raw["settings"]["batch_size"] = 2
    path.chmod(0o644)
    path.write_text(json.dumps(raw))
    with pytest.raises(ContractError, match="comparison settings"):
        execute(root)
    assert endpoint[1] == []


def test_report_regrades_private_test_rows_instead_of_trusting_summary_scores(tmp_path, endpoint):
    root = tmp_path / "study"
    setup(root, endpoint, modes=("offline",), variants=("combee_fixed",))
    result = execute(root)
    child = root / "runs/seed-0-offline-combee_fixed"
    records = json.loads((child / "execution-0001.json").read_text())
    rollout_id = result["runs"][0]["final_test"]["rollout_ids"][0]
    rollout = next(row for row in records["rollouts"] if row["id"] == rollout_id)
    folder = child / "evaluations" / content_hash(rollout["operation_id"]).split(":")[1] / "private/batches"
    path = next(folder.glob("*.json"))
    batch = json.loads(path.read_text())
    batch["rows"][0]["score"] = 1 - batch["rows"][0]["score"]
    path.chmod(0o644)
    path.write_text(json.dumps(batch))
    with pytest.raises(ContractError, match="grade disagrees"):
        report(root)


def test_failure_after_run_commit_keeps_elapsed_unknown_without_repeating_model_calls(tmp_path, endpoint, monkeypatch):
    root = tmp_path / "study"
    setup(root, endpoint, modes=("offline",), variants=("naive_parallel",))
    original = study_timing.publish_bytes

    def fail_elapsed(path, data):
        if path.name == "elapsed.json":
            raise OSError("fixture interruption before elapsed receipt")
        return original(path, data)

    monkeypatch.setattr(study_timing, "publish_bytes", fail_elapsed)
    with pytest.raises(OSError, match="fixture interruption"):
        execute(root)
    calls = len(endpoint[1])
    monkeypatch.setattr(study_timing, "publish_bytes", original)
    result = execute(root, resume=True)
    assert len(endpoint[1]) == calls
    assert result["runs"][0]["elapsed"]["interrupted"] and result["runs"][0]["elapsed"]["wall_seconds"] is None
    assert result["aggregate"]["offline/naive_parallel"]["elapsed_seconds"] == {
        "n": 1, "known": 0, "mean": None, "sample_sd": None}
    assert execute(root, resume=True) == result and len(endpoint[1]) == calls


def test_full_pinned_formula_preparation_preserves_all_three_splits(tmp_path, endpoint):
    from proteus.benchmarks import ace_formula_data

    source = os.environ.get("PROTEUS_ACE_SOURCE")
    if not source:
        pytest.skip("actual pinned Formula data required in CI")
    path = ace_formula_data.prepare(tmp_path / "data", tmp_path / "cache", checkout=source)
    dataset = TaskDataset.from_json(path.read_text())
    assert content_hash(dataset) == ace_formula_data.DATASET_HASH
    actors = {role: ActorSpec(runtime=plugin("proteus.chat", base_url=endpoint[0], key_env="FORMULA_FIXTURE_KEY"),
                             model=f"fixture-{role}") for role in ("generator", "reflector", "curator")}
    settings = Settings(**actors, seeds=(0,), modes=("offline",))
    assert settings.eval_steps == 500
    prepare(tmp_path / "study", dataset, settings, sources=[SOURCE])
    _, _, _, runs = load(tmp_path / "study")
    for entry, recipe, _ in runs:
        assert {spec.tasks.split: len(spec.tasks.task_ids) for spec in recipe.evaluations} == {
            "train": 500, "validation": 300, "test": 200}
        assert recipe.method.options["eval_steps"] == 500
        assert recipe.reference.profile == "adapted"
    assert endpoint[1] == []


def test_hard_exit_in_second_arm_resumes_study_without_repeating_completed_work(tmp_path, endpoint):
    root = tmp_path / "study"
    setup(root, endpoint, modes=("offline",), train_count=3)
    driver = r"""
import os, sys
from pathlib import Path
from proteus.core.recovery import Journal
from proteus.examples.combee_formula import execute
root = Path(sys.argv[1])
original = Journal.call
def call(self, kind, *args, **kwargs):
    result = original(self, kind, *args, **kwargs)
    if kind == "aggregation.result" and self.services.root == root / "runs/seed-0-offline-naive_parallel":
        os._exit(86)
    return result
Journal.call = call
execute(root)
"""
    stopped = subprocess.run([sys.executable, "-c", driver, str(root)],
                             text=True, capture_output=True, timeout=90)
    assert stopped.returncode == 86, stopped.stdout + stopped.stderr
    first = root / "runs/seed-0-offline-ace"
    first_events = run_ledger(first).events()
    timing = root / "timings/seed-0-offline-ace/elapsed.json"
    original_elapsed = timing.read_bytes()
    assert (root / "timings/seed-0-offline-naive_parallel/start.json").exists()
    assert not (root / "timings/seed-0-offline-naive_parallel/elapsed.json").exists()
    assert not (root / "runs/seed-0-offline-combee_fixed").exists()

    result = execute(root, resume=True)
    assert run_ledger(first).events() == first_events and timing.read_bytes() == original_elapsed
    for row in result["runs"]:
        interrupted = row["variant"] == "naive_parallel"
        assert row["elapsed"]["interrupted"] == interrupted
        assert (row["elapsed"]["wall_seconds"] is None) == interrupted
        assert run_ledger(root / "runs" / f"seed-0-offline-{row['variant']}").balance()["active_slots"] == 0
    calls = len(endpoint[1])
    assert sum(row["ledger"]["charged"]["model_calls"] for row in result["runs"]) == calls
    assert execute(root, resume=True) == result and len(endpoint[1]) == calls
