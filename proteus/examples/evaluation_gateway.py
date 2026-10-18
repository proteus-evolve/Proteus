"""Two autonomous offline workers submit candidates to a private formal evaluator."""

import argparse
from dataclasses import replace
import hashlib
from pathlib import Path
import sys

from proteus.core.contracts import BudgetLimits, PluginRef, require
from proteus.core.methods import MethodResult
from proteus.core.plugins import EmptyOptions, Plugin, implementation_hash
from proteus.examples.candidate_evaluation import plugin
from proteus.examples.evidence_history import prepare as prepare_history, register as register_history
from proteus.examples.worker_lifecycle import wait
from proteus.recipes import load_recipe
from proteus.recipes.config import GatewaySpec, KnowledgeSpec, WorkerSpec
from proteus.workers.records import CommandWorkerOptions


class GatewayMethod:
    def run(self, services, restored_state):
        # A private evaluation exists in the same run, but is not offered to workers.
        initial = services.initial_candidate()
        services.evaluate(initial, "test", operation_id="private-test")
        gateway = services.gateway("formal")
        outputs = {}
        with gateway.serve():
            workers = [(services.worker(name), name) for name in ("a", "b")]
            refs = [(worker, worker.start("search", "Submit a candidate and inspect formal feedback.")) for worker, _ in workers]
            for index in range(2):
                result = gateway.step(f"next-{index}", timeout_seconds=20)
                require(result is not None and result.status == "completed", "expected a completed gateway evaluation")
                outputs[f"evaluation_{index}"] = services.values.publish("proteus.gateway.result@1", result)
            for worker, ref in refs:
                require(wait(worker, ref).state == "completed", "worker did not receive its formal result")
        return MethodResult(outputs=outputs)


def register(registry):
    register_history(registry)
    digest = implementation_hash({"evaluation_gateway.py": Path(__file__),
                                  "fixture.py": Path(__file__).with_name("_gateway_worker_fixture.py")})
    registry.register(Plugin(ref=PluginRef(name="example.evaluation_gateway", version="1"), kind="method",
        options=EmptyOptions, factory=lambda options: GatewayMethod(), implementation_hash=digest, source="proteus",
        capabilities=("run.journal", "run.resume"), state_schema=EmptyOptions))


def prepare(root):
    path = prepare_history(root)
    recipe = load_recipe(path)
    command = (sys.executable, str(Path(__file__).with_name("_gateway_worker_fixture.py").resolve()))
    options = CommandWorkerOptions(command=command,
        command_files={name: "sha256:" + hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in command},
        timeout_seconds=60, shutdown_grace_seconds=0.2)
    recipe = replace(recipe, name="offline-evaluation-gateway", method=plugin("example.evaluation_gateway"),
        actors={"proposer": replace(recipe.actors["proposer"], budget=BudgetLimits(max_concurrency=2))},
        workers=tuple(WorkerSpec(id=name, role="proposer", runtime=plugin("proteus.worker.command", **options.to_dict()),
                                gateway="formal") for name in ("a", "b")),
        gateways=(GatewaySpec(id="formal", members={"a": "inspect", "b": "inspect"}, evaluations=("validation",),
                             knowledge_space="research", max_evaluations_per_worker=2),),
        knowledge=(KnowledgeSpec(id="research", roles=("proposer",), evidence=recipe.stages[0].evidence),),
        budget=BudgetLimits(max_evaluations=5, max_concurrency=3))
    path.write_text(recipe.to_json() + "\n")
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    print(prepare(parser.parse_args().out))


if __name__ == "__main__":
    main()
