"""Explicit offline integration fixture; no model inference or benchmark claim."""

import argparse
import json
from pathlib import Path
import re

from proteus.core.contracts import PluginRef, RuntimeIdentity, StatePolicy, require
from proteus.core.methods import ActorResult
from proteus.core.plugins import EmptyOptions, Plugin, implementation_hash
from proteus.evaluation.records import zero_usage
from proteus.evaluation.tasks import Task, TaskDataset, TaskRecord
from proteus.examples.candidate_evaluation import plugin
from proteus.examples.meta_harness import build_recipe
from proteus.methods.meta_harness.method import register as register_method
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.config import ActorSpec, EnvironmentSpec, ExecutionSpec


MEMORY = """import json
from ..memory_system import MemorySystem
from .helper import identity
class Memory(MemorySystem):
    def __init__(self, llm):
        super().__init__(llm)
        self.state = {'rows': [], 'seen': []}
    def learn_from_batch(self, rows):
        self.state['rows'].extend(rows)
    def predict(self, text):
        self.state['seen'].append(text)
        return identity(json.loads(self.call_llm(text))['final_answer']), {'learned': len(self.state['rows'])}
    def get_state(self):
        return json.dumps(self.state, sort_keys=True)
    def set_state(self, state):
        self.state = json.loads(state)
"""


class FixtureModel:
    model_call_bound = 0

    def execute(self, spec, request, workspace):
        with (workspace / "fixture-invocations.txt").open("a") as handle:
            handle.write("model\n")
        return ActorResult(
            text=json.dumps({"final_answer": "fixture-label"}),
            usage=zero_usage(),
            runtime=RuntimeIdentity(runtime=spec.runtime.plugin),
        )


class FixtureProposer:
    model_call_bound = 0

    def execute(self, spec, request, workspace):
        task = workspace / "task"
        view = json.loads((task / "history/view.json").read_text())
        require(len(view["candidate_ids"]) >= 2, "fixture must inspect multiple baselines")
        require(all(row["split"] != "test" for row in view["entries"]), "hidden history disclosed")
        sources = [row["path"] for row in view["entries"] if row["kind"] == "artifact"]
        for path in sources:
            (task / "history" / path).read_bytes()
        iteration = int(re.search(r"directory adaptation \(iteration (\d+)\)", request.prompt)[1])
        output = task / "output"
        (output / "agents").mkdir()
        (output / "reports").mkdir()
        (output / "agents/helper.py").write_text("def identity(value):\n    return value\n")
        proposals = []
        for label, source in (
            ("learned", MEMORY),
            (
                "wrong",
                MEMORY.replace(
                    "identity(json.loads(self.call_llm(text))['final_answer'])", "'wrong'"
                ),
            ),
            ("broken", 'raise ValueError("invalid proposed code fixture")\n'),
        ):
            name = f"{label}_{iteration}"
            (output / "agents" / f"{name}.py").write_text(source)
            proposals.append(
                {
                    "name": name,
                    "file": f"agents/{name}.py",
                    "axis": "exploration",
                    "base_system": "no_memory",
                    "hypothesis": "deterministic fixture",
                    "components": ["memory"],
                }
            )
        (output / "pending_eval.json").write_text(
            json.dumps({"iteration": iteration, "candidates": proposals})
        )
        (output / "reports" / f"iteration-{iteration}.md").write_text(
            "Offline fixture; no inference.\n"
        )
        with (workspace / "fixture-invocations.txt").open("a") as handle:
            handle.write("proposer\n")
        return ActorResult(
            text="Fixture file proposals.",
            usage=zero_usage(),
            trace=tuple({"kind": "file_read", "path": "history/" + path} for path in sources),
            runtime=RuntimeIdentity(runtime=spec.runtime.plugin),
        )


def register(registry):
    digest = implementation_hash({"fixture.py": Path(__file__)})
    for name, factory, extra in (
        ("model", FixtureModel, ("request.timeout",)),
        ("proposer", FixtureProposer, ("actor.workspace",)),
    ):
        registry.register(
            Plugin(
                ref=PluginRef(name="example.meta_" + name, version="1"),
                kind="actor",
                options=EmptyOptions,
                factory=lambda options, factory=factory: factory(),
                source="proteus",
                implementation_hash=digest,
                capabilities=("actor.execute", "session.fresh", "calls.bounded", *extra),
            )
        )


def registry():
    result = builtin_registry()
    register_method(result)
    register(result)
    return result


def prepare(
    root,
    *,
    mode="offline",
    iterations=1,
    num_epochs=1,
    batch_size=2,
    seeds=(42,),
    datasets=("Symptom2Disease",),
    execution=None,
    task_count=2,
):
    pools = []
    for name in datasets:
        data = TaskDataset(
            version="offline-fixture",
            records=tuple(
                TaskRecord(
                    task=Task(
                        id=f"{split}-{i}",
                        version="fixture",
                        split=split,
                        input=f"{split} query {i}",
                    ),
                    reference={
                        "target": "fixture-label",
                        "extras": {"raw_question": f"raw {split} {i}"},
                    },
                )
                for split in ("train", "val", "test")
                for i in range(task_count)
            ),
        )
        for seed in seeds:
            orders = {
                split: tuple(row.task.id for row in data.records if row.task.split == split)
                for split in ("train", "val", "test")
            }
            pools.append((name, seed, data, orders))
    execution = execution or ExecutionSpec(
        environment=EnvironmentSpec(
            backend=plugin("proteus.local_json"),
            provider="host",
            network="unrestricted",
            lifecycle="sequence",
        ),
        state_policy=StatePolicy.SEQUENCE,
        timeout_seconds=10,
    )
    return build_recipe(
        root,
        pools,
        ActorSpec(runtime=plugin("example.meta_model")),
        ActorSpec(runtime=plugin("example.meta_proposer")),
        execution,
        iterations=iterations,
        mode=mode,
        num_epochs=num_epochs,
        batch_size=batch_size,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    print(prepare(parser.parse_args().out))


if __name__ == "__main__":
    main()
