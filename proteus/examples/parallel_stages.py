"""A dependency-free map/join/reduce recipe; the default actor is an offline echo."""

import argparse
from dataclasses import dataclass, field
from pathlib import Path

from proteus.core.contracts import BudgetLimits, OperationStatus, PluginRef, require
from proteus.core.methods import MethodResult
from proteus.core.parallel import StageJob
from proteus.core.plugins import EmptyOptions, Plugin, implementation_hash
from proteus.core.serialization import Contract
from proteus.recipes.builtins import Text, builtin_registry
from proteus.recipes.config import ActorSpec, PluginSpec, RecipeConfig, ReferenceSpec, StageSpec


@dataclass(frozen=True, kw_only=True)
class MapOptions(Contract):
    prompts: tuple[str, ...] = field(
        default=("First observation", "Second observation", "Third observation"),
        metadata={"minItems": 1},
    )
    max_workers: int = field(default=2, metadata={"minimum": 1})


class MapReduceExample:
    def __init__(self, options):
        self.options = options

    def run(self, services, restored_state):
        jobs = tuple(
            StageJob(
                key=f"input-{index}",
                stage="map",
                inputs={"prompt": services.values.publish("proteus.text@1", Text(text=prompt))},
            )
            for index, prompt in enumerate(self.options.prompts)
        )
        mapped = services.map_stages(
            jobs, operation_id="observations", max_workers=self.options.max_workers
        )
        map_ref = services.values.publish("proteus.stage_map@1", mapped)
        if not mapped.succeeded:
            return MethodResult(
                status=OperationStatus.FAILED,
                outputs={"map": map_ref},
                diagnostic="map failed; no incomplete group was reduced",
            )
        # Reduce consumes the declared input order, never worker completion order.
        responses = [
            services.values.get(result.outputs["response"]).text for result in mapped.results
        ]
        reduced = services.stage(
            "reduce",
            {"prompt": services.values.publish("proteus.text@1", Text(text="\n".join(responses)))},
            operation_id="aggregation",
        )
        return MethodResult(outputs={"map": map_ref, "response": reduced.outputs["response"]})


def register(registry):
    registry.register(
        Plugin(
            ref=PluginRef(name="example.map_reduce", version="1"),
            kind="method",
            options=MapOptions,
            factory=MapReduceExample,
            source="proteus",
            implementation_hash=implementation_hash({"parallel_stages.py": Path(__file__)}),
            capabilities=("run.journal", "run.resume"),
            state_schema=EmptyOptions,
        )
    )


def prepare(root, *, max_workers=2):
    def plugin(name):
        return PluginSpec(plugin=PluginRef(name=name, version="1"))

    registry = builtin_registry()
    register(registry)
    config = RecipeConfig(
        name="offline-parallel-stages",
        method=PluginSpec(
            plugin=PluginRef(name="example.map_reduce", version="1"),
            options={"max_workers": max_workers},
        ),
        actors={
            "learner": ActorSpec(
                runtime=plugin("proteus.echo"),
                budget=BudgetLimits(max_model_calls=0, max_concurrency=max_workers),
            )
        },
        stages=tuple(
            StageSpec(
                id=name,
                role="learner",
                operator=plugin("proteus.prompt"),
                read_scopes=("proteus.text@1",),
                write_scopes=("proteus.actor_result@1",),
            )
            for name in ("map", "reduce")
        ),
        budget=BudgetLimits(max_model_calls=0, max_concurrency=max_workers),
        reference=ReferenceSpec(
            profile="native",
            deviations=(
                "Offline echo integration example; no method reproduction or speedup claim.",
            ),
        ),
    )
    root = Path(root).resolve()
    require(not root.exists(), "example destination already exists")
    root.mkdir(parents=True)
    path = root / "recipe.json"
    path.write_text(registry.resolve(config).to_json() + "\n")
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--max-workers", type=int, default=2)
    args = parser.parse_args()
    print(prepare(args.out, max_workers=args.max_workers))


if __name__ == "__main__":
    main()
