"""Prepare the model-free Combee-ACE offline/online integration fixture."""

import argparse
from dataclasses import replace
from pathlib import Path

from proteus.core.contracts import BudgetLimits
from proteus.examples.ace_playbook import prepare as prepare_ace
from proteus.examples.candidate_evaluation import plugin
from proteus.methods.ace.batch import CombeeOptions, SampleOptions
from proteus.recipes.config import StageSpec, WorkflowSpec, load_recipe


def prepare(root, *, mode="offline", workers=3, batch_size=2):
    path = prepare_ace(root, mode=mode)
    recipe = load_recipe(path)
    common = {
        key: value
        for key, value in recipe.method.options.items()
        if key not in ("curation_stage", "curator_frequency")
    }
    options = CombeeOptions.from_dict(
        {
            **common,
            "sample_workflow": "learn-sample",
            "aggregation_stage": "aggregate",
            "batch_size": batch_size,
            "max_workers": workers,
        }
    )
    sample = SampleOptions(
        train=options.train,
        reflection_stage=options.reflection_stage,
        max_num_rounds=options.max_num_rounds,
        no_ground_truth=options.no_ground_truth,
        json_mode=options.json_mode,
    )
    recipe = replace(
        recipe,
        name=f"combee-ace-{mode}-wiring",
        method=plugin("proteus.combee_ace", **options.to_dict()),
        actors={
            role: replace(actor, budget=BudgetLimits(max_concurrency=workers))
            for role, actor in recipe.actors.items()
        },
        stages=(
            recipe.stages[0],
            StageSpec(
                id="aggregate",
                operator=plugin("proteus.prompt"),
                role="curator",
                read_scopes=("proteus.text@1",),
                write_scopes=("proteus.actor_result@1",),
            ),
        ),
        workflows=(
            WorkflowSpec(
                id="learn-sample",
                operator=plugin("proteus.combee_ace_sample", **sample.to_dict()),
                stages=("reflect",),
                evaluations=("train",),
            ),
        ),
        execution=replace(recipe.execution, max_workers=workers),
        budget=BudgetLimits(max_evaluations=120, max_model_calls=160, max_concurrency=workers),
        reference=replace(
            recipe.reference,
            deviations=(
                "Synthetic tasks and deterministic actors: no model or benchmark result.",
                "Fixed-batch ACEBatch control flow with bounded shared run/role concurrency.",
                "Managed transport stops on failures; no API-error sentinel scores or unbounded retries.",
                "Immutable validated playbooks and explicit phase-3 rollback; no optional embedding analyzer.",
            ),
        ),
    )
    path.write_text(recipe.to_json() + "\n")
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--mode", choices=("offline", "online"), default="offline")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--batch-size", type=int, default=2)
    args = parser.parse_args()
    print(prepare(args.out, mode=args.mode, workers=args.workers, batch_size=args.batch_size))


if __name__ == "__main__":
    main()
