"""Prepare a model-free GEPA wiring fixture; this is not a benchmark reproduction."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from proteus.artifacts.codecs import TextComponents
from proteus.artifacts.store import ArtifactStore
from proteus.core.contracts import BudgetLimits, FeedbackVisibility, PluginRef, RuntimeIdentity, Usage, require
from proteus.core.methods import ActorResult
from proteus.core.plugins import Plugin, implementation_hash
from proteus.core.serialization import Contract, content_hash
from proteus.evaluation.tasks import Task, TaskDataset, TaskRecord
from proteus.examples.candidate_evaluation import plugin
from proteus.methods.gepa.method import GepaOptions
from proteus.methods.gepa.reference import REPOSITORY, REVISION
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.config import (
    ActorSpec, EnvironmentSpec, EvaluationSpec, ExecutionSpec, RecipeConfig, ReferenceSpec,
    StageSpec, SubjectSpec, TaskSuiteSpec,
)


@dataclass(frozen=True, kw_only=True)
class OfflineOptions(Contract):
    role: Literal["reflection", "subject"]
    proposal: str = "Return 1."


class OfflineActor:
    model_call_bound = 0

    def __init__(self, options):
        self.options = options

    def execute(self, spec, request, workspace):
        # Append even if called again in the same workspace: recovery tests can
        # detect real invocations independently of reported zero model usage.
        with (workspace / "fixture-invocations.txt").open("a") as stream:
            stream.write(self.options.role + "\n")
        answer = ("```\n" + self.options.proposal + "\n```") if self.options.role == "reflection" else (
            "1" if request.prompt.startswith("Return 1.\n\n") else "0")
        return ActorResult(text=answer, runtime=RuntimeIdentity(runtime=spec.runtime.plugin),
            usage=Usage(model_calls=0, input_tokens=0, output_tokens=0, cost_usd=0),
            trace=({"kind": "offline-fixture", "role": self.options.role},))


def validate_actor(spec, options):
    require(spec.model is None and not spec.model_settings and not spec.tools,
            "offline GEPA fixture does not run a model or tools")


def register(registry):
    registry.register(Plugin(ref=PluginRef(name="example.gepa_offline", version="1"), kind="actor",
        options=OfflineOptions, factory=OfflineActor, implementation_hash=implementation_hash({"example": Path(__file__)}),
        source="proteus", capabilities=("session.fresh", "actor.execute", "calls.bounded"), validate=validate_actor))


def prepare(root):
    root = Path(root).resolve()
    require(not root.exists() or not any(root.iterdir()), "example directory already contains data")
    root.mkdir(parents=True, exist_ok=True)
    dataset = TaskDataset(version="gepa-wiring-fixture-1", records=tuple(
        TaskRecord(task=Task(id=f"{split}-{i}", version="1", split=split, input={"example": i}), reference="1")
        for split in ("train", "validation", "test") for i in range(2)))
    data = root / "tasks.json"
    data.write_text(dataset.to_json() + "\n")
    store = ArtifactStore(root / "artifact-store", builtin_registry())
    initial = store.put(PluginRef(name="proteus.text_components", version="1"),
                        TextComponents(components={"system.txt": "Return 0."}))
    evaluations = []
    for split in ("train", "validation", "test"):
        evaluations.append(EvaluationSpec(id=split, evaluator=plugin("proteus.exact_match"),
            tasks=TaskSuiteSpec(suite=plugin("proteus.json_tasks", path=str(data)), dataset_version=dataset.version,
                dataset_hash=content_hash(dataset), split=split, task_ids=tuple(f"{split}-{i}" for i in range(2))),
            purpose="optimization" if split == "train" else split, selection_eligible=split != "test",
            visibility=FeedbackVisibility.HIDDEN if split == "test" else FeedbackVisibility.OBSERVED,
            feedback_timing="never" if split == "test" else "after_batch"))
    options = GepaOptions(train="train", validation="validation", reflection_stage="reflect", max_metric_calls=10,
                          test_evaluations=("test",))
    recipe = RecipeConfig(name="gepa-offline-wiring", method=plugin("proteus.gepa", **options.to_dict()),
        reference=ReferenceSpec(profile="reference", code=REPOSITORY, revision=REVISION,
            deviations=("Deterministic offline actors and synthetic tasks; no model or benchmark result.",
                        "Single mutation, full validation, instance frontier, no merge/cache, text components.")),
        actors={role: ActorSpec(runtime=plugin("example.gepa_offline", role=role)) for role in ("subject", "reflection")},
        subject=SubjectSpec(runner=plugin("proteus.prompt_subject"), initial_artifact=initial, actor="subject"),
        execution=ExecutionSpec(environment=EnvironmentSpec(backend=plugin("proteus.local_json"), provider="host",
                                                            network="unrestricted")),
        stages=(StageSpec(id="reflect", operator=plugin("proteus.prompt"), role="reflection",
            input_types={"prompt": "proteus.text@1"}, output_types={"response": "proteus.actor_result@1"},
            read_scopes=("proteus.text@1",), write_scopes=("proteus.actor_result@1",)),),
        search={"parent": plugin("proteus.gepa_parent"), "frontier": plugin("proteus.gepa_frontier"),
                "final": plugin("proteus.gepa_best")}, evaluations=tuple(evaluations),
        budget=BudgetLimits(max_evaluations=19, max_model_calls=32))
    path = root / "recipe.json"
    path.write_text(recipe.to_json() + "\n")
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    print(prepare(parser.parse_args().out))


if __name__ == "__main__":
    main()
