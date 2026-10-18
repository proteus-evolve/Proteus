"""Naive parallel control changes aggregation only, with no hidden reducer."""

from dataclasses import replace

import pytest

from proteus.core.serialization import ContractError
from proteus.examples.ace_playbook import register as fixture
from proteus.examples.combee_ace import prepare
from proteus.methods.ace.batch import CombeeOptions, register
from proteus.operators.combee.grouping import Grouping
from proteus.recipes import load_recipe
from proteus.recipes.builtins import builtin_registry
from test_v1_ace_method import run
from test_v1_evaluation import plugin


def setup(root, mode):
    recipe = load_recipe(prepare(root, mode=mode, workers=3))
    options = replace(CombeeOptions.from_dict(recipe.method.options), aggregation_mode="single_pass",
                      grouping=Grouping(profile="ace", duplication_factor=1, num_groups=1))
    registry = builtin_registry()
    register(registry)
    fixture(registry)
    return registry, replace(recipe, method=plugin("proteus.combee_ace", **options.to_dict()))


@pytest.mark.parametrize("mode", ["offline", "online"])
def test_single_pass_preserves_samples_and_records_exactly_one_curation_per_batch(tmp_path, mode):
    root = tmp_path / "study"
    registry, recipe = setup(root, mode)
    result, records = run(root, registry, recipe)
    assert result.status.value == "completed", result.diagnostic
    values = {row["ref"]["id"]: row["value"] for row in records["values"]}
    summary = next(row["value"] for row in records["values"] if row["ref"]["kind"] == "proteus.combee.ace_summary@1")
    batches = [values[ref["id"]] for ref in summary["batches"]]
    assert [task for batch in batches for task in batch["task_ids"]] == [f"train-{i}" for i in range(3)]
    assert len([call for call in records["actor_calls"] if call["role"] == "curator"]) == len(batches)
    for batch in batches:
        aggregation = values[batch["aggregation"]["id"]]
        assert aggregation["mode"] == "single_pass" and aggregation["reducer"] is None
        assert len(aggregation["proposals"]) == len(aggregation["waves"]) == 1
        assert aggregation["delta"]["records"] == aggregation["proposals"][0]["operations"]
        plan = values[aggregation["plan"]["id"]]
        assert plan["rng_before"] == plan["rng_after"]
        assert [[(row["source_index"], row["copy_index"]) for row in group] for group in plan["groups"]] == [
            [(i, 0) for i in range(len(batch["task_ids"]))]]
        wave = values[aggregation["waves"][0]["id"]]
        assert len(wave["responses"]) == len(wave["prompts"]) == 1
        prompt = values[wave["prompts"][0]["id"]]["text"]
        for ref in plan["records"]:
            item = values[ref["id"]]
            assert prompt.count(f"[Sample {item['sample_number']}] {item['reflection']}") == 1
    resumed, again = run(root, registry, recipe, resume=True)
    assert resumed == result and records["actor_calls"] == again["actor_calls"]
    assert sum(len(path.read_text().splitlines()) for path in (root / "run").rglob("fixture-invocations.txt")) == len(records["actor_calls"])


def test_single_pass_rejects_unused_combee_grouping_before_running(tmp_path):
    registry, recipe = setup(tmp_path / "study", "offline")
    options = replace(CombeeOptions.from_dict(recipe.method.options), grouping=Grouping(profile="ace"))
    with pytest.raises(ContractError, match="one group with no duplication"):
        registry.resolve(replace(recipe, method=plugin("proteus.combee_ace", **options.to_dict())))
    assert not (tmp_path / "study/run").exists()


def test_naive_single_group_is_distinct_from_combee_single_group(tmp_path):
    registry, recipe = setup(tmp_path / "naive", "offline")
    result, naive = run(tmp_path / "naive", registry, recipe)
    assert result.status.value == "completed"
    registry, recipe = setup(tmp_path / "combee", "offline")
    options = replace(CombeeOptions.from_dict(recipe.method.options), aggregation_mode="combee")
    result, combee = run(tmp_path / "combee", registry,
                        replace(recipe, method=plugin("proteus.combee_ace", **options.to_dict())))
    assert result.status.value == "completed"
    assert len([row for row in combee["actor_calls"] if row["role"] == "curator"]) == 2 * len(
        [row for row in naive["actor_calls"] if row["role"] == "curator"])
    assert [row["request"]["prompt"] for row in naive["actor_calls"] if row["role"] == "reflector"] == [
        row["request"]["prompt"] for row in combee["actor_calls"] if row["role"] == "reflector"]
