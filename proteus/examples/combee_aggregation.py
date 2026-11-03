"""Run a recorded Combee aggregation fixture; no inference or benchmark claim."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Literal

from proteus.artifacts.store import ArtifactStore
from proteus.core.contracts import BudgetLimits, PluginRef, RuntimeIdentity, require
from proteus.core.methods import ActorResult, MethodResult
from proteus.core.plugins import EmptyOptions, Plugin, implementation_hash
from proteus.core.serialization import Contract, content_hash
from proteus.evaluation.records import zero_usage
from proteus.methods.ace.playbook import CODEC, Playbook, register as register_playbook
from proteus.methods.ace.roles import register as register_roles
from proteus.operators.combee import register as register_aggregation
from proteus.operators.combee.ace import AceRecord, aggregate_ace
from proteus.operators.combee.gepa import aggregate_gepa
from proteus.operators.combee.grouping import Grouping
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.config import (
    ActorSpec,
    EnvironmentSpec,
    ExecutionSpec,
    PluginSpec,
    RecipeConfig,
    ReferenceSpec,
    StageSpec,
    SubjectSpec,
)


@dataclass(frozen=True, kw_only=True)
class Options(Contract):
    family: Literal["ace", "gepa"] = "ace"
    samples: int = field(default=9, metadata={"minimum": 0})
    duplication_factor: int = field(default=2, metadata={"minimum": 1})
    max_workers: int = field(default=3, metadata={"minimum": 1})
    num_groups: int | None = field(default=None, metadata={"minimum": 1})
    batch_reflection: bool = True
    components: tuple[str, ...] = ("instruction",)
    repeat: int = field(default=1, metadata={"minimum": 1})


@dataclass(frozen=True, kw_only=True)
class FixtureOptions(Contract):
    family: Literal["ace", "gepa"] = "ace"
    responses: Mapping[str, str] = field(default_factory=dict)
    default_response: str | None = None


def response(family, prompt):
    stamp = content_hash(prompt)[7:19]
    if family == "gepa":
        return f"```instruction {stamp}```"
    return json.dumps(
        {
            "reasoning": "offline fixture",
            "operations": [
                {"type": "ADD", "section": "OTHERS", "content": f"fixture {stamp}"},
                {"type": "UPDATE", "content": "unsupported; must not apply"},
            ],
        }
    )


class FixtureActor:
    model_call_bound = 0

    def __init__(self, options):
        self.options = options

    def execute(self, spec, request, workspace):
        with (workspace / "fixture-invocations.txt").open("a") as stream:
            stream.write(content_hash(request.prompt) + "\n")
        text = self.options.responses.get(
            content_hash(request.prompt), self.options.default_response
        )
        return ActorResult(
            text=text if text is not None else response(self.options.family, request.prompt),
            runtime=RuntimeIdentity(runtime=spec.runtime.plugin),
            usage=zero_usage(),
            trace=({"kind": "offline-combee-aggregation-fixture"},),
        )


class AggregationExample:
    def __init__(self, options):
        self.options = options

    def run(self, services, restored_state):
        options, outputs = self.options, {}
        base = services.initial_candidate().artifact if options.family == "ace" else None
        for repeat in range(options.repeat):
            common = dict(
                stage="reflect",
                operation_id=f"aggregation-{repeat}",
                max_workers=options.max_workers,
            )
            if options.family == "ace":
                records = tuple(
                    services.values.publish(
                        "proteus.combee.ace_record@1",
                        AceRecord(
                            sample_number=i + 1,
                            reflection=f"reflection {i}",
                            context=f"context {i}",
                        ),
                    )
                    for i in range(options.samples)
                )
                result_ref = aggregate_ace(
                    services,
                    base,
                    records,
                    **common,
                    step=max(1, options.samples),
                    total_samples=max(1, options.samples),
                    grouping=Grouping(
                        profile="ace",
                        duplication_factor=options.duplication_factor,
                        num_groups=options.num_groups,
                    ),
                )
                result = services.values.get(result_ref)
                artifact = services.artifacts.apply_delta(base, result.delta)
                outputs[f"artifact-{repeat}"] = services.values.publish(
                    "proteus.artifact_ref@1", artifact
                )
            else:
                candidate = {name: f"base {name}" for name in options.components}
                # Deliberately nonalphabetic field order exercises prompt fidelity.
                records = [
                    {"trace": f"reflection {i}", "answer": str(i)} for i in range(options.samples)
                ]
                result_ref = aggregate_gepa(
                    services,
                    candidate,
                    {name: records for name in candidate},
                    options.components,
                    **common,
                    duplication_factor=options.duplication_factor,
                    batch_reflection=options.batch_reflection,
                )
            outputs[f"aggregation-{repeat}"] = result_ref
        return MethodResult(outputs=outputs)


def validate_fixture(spec, options):
    require(
        spec.model is None and not spec.model_settings and not spec.tools,
        "aggregation fixture does not call a model",
    )


def register(registry):
    register_playbook(registry)
    register_roles(registry)
    register_aggregation(registry)
    digest = implementation_hash({"example": Path(__file__)})
    registry.register(
        Plugin(
            ref=PluginRef(name="example.combee_aggregation", version="1"),
            kind="method",
            options=Options,
            factory=AggregationExample,
            source="proteus",
            implementation_hash=digest,
            capabilities=("run.journal", "run.resume"),
            state_schema=EmptyOptions,
        )
    )
    registry.register(
        Plugin(
            ref=PluginRef(name="example.combee_fixture", version="1"),
            kind="actor",
            options=FixtureOptions,
            factory=FixtureActor,
            source="proteus",
            implementation_hash=digest,
            capabilities=("actor.execute", "actor.parallel", "session.fresh", "calls.bounded"),
            validate=validate_fixture,
        )
    )


def prepare(root, *, family="ace", samples=9):
    options = Options(family=family, samples=samples)

    def plugin(name, **options):
        return PluginSpec(plugin=PluginRef(name=name, version="1"), options=options)

    registry = builtin_registry()
    register(registry)
    root = Path(root).resolve()
    require(not root.exists(), "example destination already exists")
    root.mkdir(parents=True)
    base = (
        ArtifactStore(root / "artifact-store", registry).put(CODEC, Playbook.from_text())
        if family == "ace"
        else None
    )
    config = RecipeConfig(
        name=f"offline-combee-{family}-aggregation",
        seed=42,
        method=plugin("example.combee_aggregation", **options.to_dict()),
        actors={
            "learner": ActorSpec(
                runtime=plugin("example.combee_fixture", family=family),
                budget=BudgetLimits(max_model_calls=0, max_concurrency=3),
            )
        },
        stages=(
            StageSpec(
                id="reflect",
                role="learner",
                operator=plugin("proteus.prompt"),
                read_scopes=("proteus.text@1",),
                write_scopes=("proteus.actor_result@1",),
            ),
        ),
        subject=SubjectSpec(
            runner=plugin("proteus.ace_subject"), initial_artifact=base, actor="learner"
        )
        if base
        else None,
        execution=ExecutionSpec(
            environment=EnvironmentSpec(
                backend=plugin("proteus.local_json"), provider="host", network="unrestricted"
            )
        )
        if base
        else None,
        budget=BudgetLimits(max_model_calls=0, max_concurrency=3),
        reference=ReferenceSpec(
            profile="native",
            deviations=(
                "Offline aggregation fixture; no generation, task evaluation, dynamic controller or benchmark claim.",
            ),
        ),
    )
    path = root / "recipe.json"
    path.write_text(registry.resolve(config).to_json() + "\n")
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--family", choices=("ace", "gepa"), default="ace")
    parser.add_argument("--samples", type=int, default=9)
    args = parser.parse_args()
    print(prepare(args.out, family=args.family, samples=args.samples))


if __name__ == "__main__":
    main()
