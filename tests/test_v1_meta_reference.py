"""Method, data, baseline prompts, selection and state against actual upstream code."""

from dataclasses import replace
import ast
import importlib
import json
import os
from pathlib import Path

import pytest

from proteus.core.contracts import OperationStatus
from proteus.core.serialization import content_hash
from proteus.examples.meta_harness_fixture import MEMORY
from proteus.execution.code_records import CodeState
from proteus.methods.meta_harness import reference as vendored
from proteus.methods.meta_harness.data import DATASET_HASHES, DIRECTORIES, SIZES, dataset
from proteus.methods.meta_harness.records import (
    BenchmarkResult,
    DatasetProfile,
    final_systems,
    frontier,
)
from proteus.methods.meta_harness.reference.data.evaluators import get_evaluator
from meta_harness_reference import benchmark_functions, checked, reference
from test_v1_meta_harness import run, setup, values


@pytest.fixture
def source():
    source = os.environ.get("PROTEUS_META_HARNESS_SOURCE")
    if not source:
        pytest.skip("pinned Meta-Harness source required in CI")
    return Path(source)


@pytest.mark.parametrize(
    "mode,epochs,batch_size",
    [("offline", 1, 1), ("offline", 3, 2), ("online", 1, 1), ("online", 1, 2)],
)
def test_inner_loop_matches_reference_memory_states_prompts_and_scores(
    tmp_path, source, mode, epochs, batch_size
):
    root = tmp_path / "study"
    registry, recipe = setup(root, mode=mode, num_epochs=epochs, batch_size=batch_size)
    result, records = run(root, registry, recipe)
    assert result.status == OperationStatus.COMPLETED, result.diagnostic
    raw = json.loads((root / "Symptom2Disease-42.json").read_text())
    samples = {
        split: [
            dict(
                input=row["task"]["input"],
                target=row["reference"]["target"],
                **row["reference"]["extras"],
            )
            for row in raw["records"]
            if row["task"]["split"] == split
        ]
        for split in ("train", "val", "test")
    }
    refs = {row["ref"]["id"]: row["value"] for row in records["values"]}
    with reference(source, tmp_path / "reference") as (inner, api):
        for name, candidate_id in (
            ("no_memory", "initial"),
            ("fewshot_all", "fewshot_all"),
            ("learned_1", "learned_1"),
        ):
            prompts = []

            def model(prompt):
                prompts.append(prompt)
                return json.dumps({"final_answer": "fixture-label"})

            if name == "learned_1":
                namespace = {"__package__": "meta_reference_fixture.agents"}
                exec(MEMORY, namespace)
                factory = namespace["Memory"]
            else:
                module = importlib.import_module("meta_reference_fixture.agents." + name)
                factory = module.NoMemory if name == "no_memory" else module.FewShotAll
            memory = factory(model)
            evaluator = api.get_evaluator("Symptom2Disease")
            inner.run_inner_loop(
                memory,
                samples["train"],
                evaluator,
                mode=mode,
                num_epochs=epochs,
                batch_size=batch_size,
                max_workers=1,
                val_examples=samples["val"],
                skip_train_eval=True,
            )
            saved = memory.get_state()
            expected = inner.evaluate_memory(memory, samples["val"], evaluator, max_workers=1)
            actual = next(
                row for row in values(records, "proteus.meta.benchmark@1") if row["system"] == name
            )
            assert actual["memory"]["value"] == saved
            assert (actual["correct"], actual["total"], actual["context_chars"]) == (
                expected["correct"],
                expected["total"],
                expected["avg_context_len"],
            )
            observed = [
                event["prompt"]
                for rollout in records["rollouts"]
                if rollout["candidate_id"] == candidate_id and rollout["split"] != "test"
                for trace in rollout["trace"]
                for event in refs[trace["id"]]["events"]
                if event["kind"] == "model"
            ]
            assert observed == prompts


def test_full_data_prompts_order_and_vendored_function_bodies_match_reference(tmp_path, source):
    package = Path(vendored.__file__).parent
    for name in (
        "data/constants.py",
        "data/evaluators.py",
        "memory_system.py",
        "agents/no_memory.py",
        "agents/fewshot_all.py",
        "agents/fewshot_memory.py",
    ):
        assert (package / name).read_bytes() == (
            source / "reference_examples/text_classification" / name
        ).read_bytes()
    original = {
        n.name: ast.dump(n)
        for n in ast.parse(checked(source, "data/loaders.py")).body
        if isinstance(n, ast.FunctionDef)
    }
    for node in ast.parse((package / "data/loaders.py").read_text()).body:
        if isinstance(node, ast.FunctionDef):
            assert ast.dump(node) == original[node.name]
    assert (package.parent / "PROPOSER.md").read_bytes() == (
        source / "reference_examples/text_classification/.claude/skills/meta-harness/SKILL.md"
    ).read_bytes()
    with reference(source, tmp_path / "reference") as (_, api):
        for name in DIRECTORIES:
            for seed in (0, 42):
                data, orders, _ = dataset(source, name, seed)
                assert content_hash(data) == DATASET_HASHES[name]
                train, val, test, evaluator = api.load_dataset_splits_3way(
                    name, *SIZES[name], shuffle_seed=seed
                )
                by_id = {row.task.id: row for row in data.records}
                for split, expected in zip(("train", "val", "test"), (train, val, test)):
                    actual = [
                        dict(
                            input=by_id[key].task.input,
                            target=by_id[key].reference["target"],
                            **by_id[key].reference["extras"],
                        )
                        for key in orders[split]
                    ]
                    assert actual == expected
                    for row in actual:
                        for prediction in (row["target"], "incorrect fixture", ""):
                            assert get_evaluator(name)(prediction, row["target"]) == evaluator(
                                prediction, row["target"]
                            )


def test_seed_aggregation_frontier_and_final_set_match_actual_reference(tmp_path, source):
    datasets = ("USPTO", "Symptom2Disease", "LawBench")
    profiles = tuple(
        DatasetProfile(name=name, seed=seed, train="train", validation="val", test="test")
        for name in datasets
        for seed in (41, 42)
    )
    functions = benchmark_functions(source, datasets, (41, 42))
    results = []
    # Includes weighted unequal seed sizes, zero context and exact metric ties.
    for i, system in enumerate(("baseline", "compact", "tied", "accurate")):
        for j, profile in enumerate(profiles):
            total = 10 + j
            correct = total if i == 3 else total // 2
            context = 1000 if i == 3 else (0 if i in (1, 2) else 100)
            row = BenchmarkResult(
                system=system,
                candidate_id=system,
                dataset=profile.name,
                seed=profile.seed,
                memory=CodeState(artifact_hash="sha256:" + "0" * 64),
                complete=True,
                correct=correct,
                total=total,
                context_chars=context,
            )
            results.append(row)
            path = functions["run_dir"](tmp_path, profile.name, system, "fixture", profile.seed)
            path.mkdir(parents=True)
            (path / "val.json").write_text(
                json.dumps(
                    {
                        "accuracy": correct / total,
                        "correct": correct,
                        "total": total,
                        "memory_context_chars": context,
                    }
                )
            )
    functions["print_frontier"](tmp_path, tmp_path)
    expected = json.loads((tmp_path / "frontier_val.json").read_text())
    actual = frontier(results, profiles)
    # Source file traversal has unspecified exact-tie order; our declared name
    # ordering is deterministic. Both tied points must survive Pareto selection.
    assert sorted(actual["_pareto"], key=lambda x: x["system"]) == sorted(
        expected["_pareto"], key=lambda x: x["system"]
    )
    assert {key: value for key, value in actual.items() if key != "_pareto"} == {
        key: value for key, value in expected.items() if key != "_pareto"
    }
    assert final_systems(("baseline",), actual) == ("accurate", "baseline", "compact", "tied")
    failed = [
        replace(row, complete=False, correct=None, total=None, context_chars=None)
        if row.system == "accurate" and row.dataset == "USPTO"
        else row
        for row in results
    ]
    assert "accurate" not in final_systems(("baseline",), frontier(failed, profiles))
