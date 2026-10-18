"""Prepare a model-free, fixed-batch Combee GEPA recipe and recovery fixture."""

from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path

from proteus.core.contracts import BudgetLimits, PluginRef
from proteus.core.plugins import Plugin, implementation_hash
from proteus.core.serialization import content_hash
from proteus.evaluation.tasks import Task, TaskDataset, TaskRecord
from proteus.examples.candidate_evaluation import plugin
from proteus.examples.gepa_prompt import OfflineActor, OfflineOptions, validate_actor
from proteus.examples.gepa_prompt import prepare as prepare_gepa
from proteus.methods.combee_gepa import CombeeGepaOptions, register as register_method
from proteus.methods.gepa.method import evaluation_bound
from proteus.recipes.config import load_recipe


def register(registry):
    register_method(registry)
    registry.register(
        Plugin(
            ref=PluginRef(name="example.combee_gepa_offline", version="1"),
            kind="actor",
            options=OfflineOptions,
            factory=OfflineActor,
            source="proteus",
            implementation_hash=implementation_hash(
                {"example": Path(__file__), "actor": Path(__file__).with_name("gepa_prompt.py")}
            ),
            capabilities=("session.fresh", "actor.execute", "calls.bounded", "actor.parallel"),
            validate=validate_actor,
        )
    )


def prepare(root):
    path = prepare_gepa(root)
    root = path.parent
    recipe = load_recipe(path)
    dataset = TaskDataset(
        version="combee-gepa-wiring-fixture-1",
        records=tuple(
            TaskRecord(
                task=Task(id=f"{split}-{i}", version="1", split=split, input={"example": i}),
                reference="1",
            )
            for split, size in (("train", 9), ("validation", 2), ("test", 2))
            for i in range(size)
        ),
    )
    (root / "tasks.json").write_text(dataset.to_json() + "\n")
    evaluations = tuple(
        replace(
            item,
            tasks=replace(
                item.tasks,
                dataset_version=dataset.version,
                dataset_hash=content_hash(dataset),
                task_ids=tuple(
                    record.task.id for record in dataset.records if record.task.split == item.id
                ),
            ),
        )
        for item in recipe.evaluations
    )
    options = CombeeGepaOptions(
        train="train",
        validation="validation",
        reflection_stage="reflect",
        max_metric_calls=22,
        test_evaluations=("test",),
    )
    recipe = replace(
        recipe,
        name="combee-gepa-offline-wiring",
        evaluations=evaluations,
        method=plugin("proteus.combee_gepa", **options.to_dict()),
        reference=replace(
            recipe.reference,
            profile="adapted",
            paper="https://arxiv.org/abs/2604.04247",
            deviations=(
                "Fixed-batch ComBEE operator f1cfdee on GEPA engine 8b0ce6c; engine pin is the code reference.",
                "Serial formal task evaluation; parallel fresh reflection calls. No dynamic batch controller.",
                "Single mutation, full validation, instance frontier, no merge/cache, text components.",
                "Deterministic offline actors and synthetic tasks; no model or benchmark result.",
            ),
        ),
        actors={
            role: replace(
                spec,
                runtime=plugin("example.combee_gepa_offline", role=role),
                budget=BudgetLimits(max_model_calls=0, max_concurrency=3),
            )
            for role, spec in recipe.actors.items()
        },
        budget=BudgetLimits(
            max_evaluations=evaluation_bound(options, 2) + 2, max_model_calls=0, max_concurrency=3
        ),
    )
    path.write_text(recipe.to_json() + "\n")
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    print(prepare(parser.parse_args().out))


if __name__ == "__main__":
    main()
