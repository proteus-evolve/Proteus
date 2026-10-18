"""Prepare a deterministic ACE mechanism fixture; no model or benchmark claim."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from proteus.artifacts.store import ArtifactStore
from proteus.core.contracts import BudgetLimits, FeedbackVisibility, Outcome, PluginRef, RuntimeIdentity, require
from proteus.core.methods import ActorResult
from proteus.core.plugins import EmptyOptions, Plugin, implementation_hash
from proteus.core.serialization import Contract, content_hash
from proteus.evaluation.records import Feedback, Grade, zero_usage
from proteus.evaluation.tasks import Task, TaskDataset, TaskRecord
from proteus.examples.candidate_evaluation import plugin
from proteus.methods.ace.method import AceOptions, register as register_method
from proteus.methods.ace.playbook import CODEC, REPOSITORY, REVISION, Playbook
from proteus.methods.ace.roles import Generation
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.config import ActorSpec, EnvironmentSpec, EvaluationSpec, ExecutionSpec, RecipeConfig, ReferenceSpec, StageSpec, SubjectSpec, TaskSuiteSpec


@dataclass(frozen=True, kw_only=True)
class OfflineOptions(Contract):
    role: Literal["generator", "reflector", "curator"]
    behavior: Literal["learn", "correct", "wrong"] = "learn"
    invalid_curation: bool = False


def response(role, prompt, *, behavior="learn", invalid_curation=False):
    """Shared only by the fixture actor and independently loaded upstream roles."""
    if role == "generator":
        correct = behavior == "correct" or (behavior == "learn" and
            ("fixture learned" in prompt or "fixture reflection" in prompt))
        return json.dumps({"reasoning": "[misc-00001]", "final_answer": "1" if correct else "0",
                           "bullet_ids": ["misc-00001", "misc-00001"]})
    if role == "reflector":
        return json.dumps({"reflection": "fixture reflection", "bullet_tags": [
            {"id": "misc-00001", "tag": "harmful"}, {"id": "misc-00001", "tag": "helpful"}]})
    if invalid_curation:
        return "not a curator object"
    return json.dumps({"reasoning": "fixture", "operations": [
        {"type": "ADD", "section": "OTHERS", "content": "fixture learned"},
        {"type": "UPDATE", "bullet_id": "misc-00001", "content": "unsupported operation"}]})


class OfflineActor:
    model_call_bound = 0

    def __init__(self, options):
        self.options = options

    def execute(self, spec, request, workspace):
        with (workspace / "fixture-invocations.txt").open("a") as stream:
            stream.write(self.options.role + "\n")
        return ActorResult(text=response(self.options.role, request.prompt,
            behavior=self.options.behavior, invalid_curation=self.options.invalid_curation),
            runtime=RuntimeIdentity(runtime=spec.runtime.plugin), usage=zero_usage(),
            trace=({"kind": "ace-offline-fixture", "role": self.options.role},))


class ExactAnswer:
    def grade(self, task, rollout, reference):
        require(type(reference) is str, "ACE fixture reference must be text")
        generation = Generation.from_dict(rollout.output)
        correct = generation.answer == reference
        return Grade(status=Outcome.SUCCEEDED if correct else Outcome.TASK_FAILED, score=float(correct),
            feedback=Feedback(value={"correct": correct, "ground_truth": reference}), usage=zero_usage())


def validate_actor(spec, options):
    require(spec.model is None and not spec.model_settings and not spec.tools,
            "ACE wiring fixture does not call a model")


def register(registry):
    digest = implementation_hash({"example": Path(__file__)})
    registry.register(Plugin(ref=PluginRef(name="example.ace_offline", version="1"), kind="actor",
        options=OfflineOptions, factory=OfflineActor, source="proteus", implementation_hash=digest,
        capabilities=("session.fresh", "actor.execute", "calls.bounded"), validate=validate_actor))
    registry.register(Plugin(ref=PluginRef(name="example.ace_exact", version="1"), kind="evaluator",
        options=EmptyOptions, factory=lambda options: ExactAnswer(), source="proteus", implementation_hash=digest,
        capabilities=("evaluator.deterministic",)))


def prepare(root, *, mode="offline"):
    root = Path(root).resolve()
    require(not root.exists() or not any(root.iterdir()), "example directory already contains data")
    root.mkdir(parents=True, exist_ok=True)
    registry = builtin_registry()
    register_method(registry)
    register(registry)
    dataset = TaskDataset(version="ace-wiring-fixture-1", records=tuple(
        TaskRecord(task=Task(id=f"{split}-{i}", version="1", split=split,
            input={"question": f"Solve fixture {split}-{i}.", "context": f"Context {split}-{i}."}),
            reference="1" if split != "test" else "hidden-reference")
        for split in ("train", "validation", "test") for i in range(3)))
    data = root / "tasks.json"
    data.write_text(dataset.to_json() + "\n")
    initial = ArtifactStore(root / "artifact-store", registry).put(CODEC, Playbook.from_text())
    evaluations = []
    for key, split in (("train", "train"), ("validation", "validation"), ("test", "test")) if mode == "offline" else (
        ("train", "train"), ("stream", "train")):
        purpose = "optimization" if key == "train" else "validation" if key == "validation" else "test"
        evaluations.append(EvaluationSpec(id=key, evaluator=plugin("example.ace_exact"),
            tasks=TaskSuiteSpec(suite=plugin("proteus.json_tasks", path=str(data)), dataset_version=dataset.version,
                dataset_hash=content_hash(dataset), split=split, task_ids=tuple(f"{split}-{i}" for i in range(3))),
            purpose=purpose, selection_eligible=purpose != "test",
            visibility=FeedbackVisibility.HIDDEN if purpose == "test" else FeedbackVisibility.OBSERVED,
            feedback_timing="never" if purpose == "test" else "after_batch" if purpose == "validation" else "after_prediction"))
    options = AceOptions(mode=mode, train="train", validation="validation" if mode == "offline" else None,
        stream="stream" if mode == "online" else None, test_evaluations=("test",) if mode == "offline" else (),
        reflection_stage="reflect", curation_stage="curate", eval_steps=2, online_eval_frequency=2)
    recipe = RecipeConfig(name=f"ace-{mode}-wiring", method=plugin("proteus.ace", **options.to_dict()),
        reference=ReferenceSpec(profile="adapted", code=REPOSITORY, revision=REVISION,
            deviations=("Synthetic tasks and deterministic actors: no model or benchmark result.",
                        "Managed transport stops on infrastructure failure; no upstream retries or incorrect-answer fallback.",
                        "Serial evaluation, immutable validated playbooks, no optional embedding analyzer.")),
        actors={role: ActorSpec(runtime=plugin("example.ace_offline", role=role)) for role in ("generator", "reflector", "curator")},
        subject=SubjectSpec(runner=plugin("proteus.ace_subject"), initial_artifact=initial, actor="generator"),
        execution=ExecutionSpec(environment=EnvironmentSpec(backend=plugin("proteus.local_json"),
            provider="host", network="unrestricted")),
        stages=tuple(StageSpec(id=name, operator=plugin(f"proteus.ace_{name}"), role=role,
            read_scopes=(f"proteus.ace.{port}_input@1",), write_scopes=(f"proteus.ace.{port}@1",))
            for name, role, port in (("reflect", "reflector", "reflection"), ("curate", "curator", "curation"))),
        evaluations=tuple(evaluations), budget=BudgetLimits(max_evaluations=40, max_model_calls=60))
    path = root / "recipe.json"
    path.write_text(recipe.to_json() + "\n")
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--mode", choices=("offline", "online"), default="offline")
    args = parser.parse_args()
    print(prepare(args.out, mode=args.mode))


if __name__ == "__main__":
    main()
