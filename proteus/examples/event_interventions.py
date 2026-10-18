"""Two offline workers receive formal-score-triggered native interventions."""

import argparse
from dataclasses import replace
import hashlib
from pathlib import Path
import sys

from proteus.core.contracts import PluginRef, require
from proteus.core.methods import MethodResult
from proteus.core.plugins import EmptyOptions, Plugin, implementation_hash
from proteus.examples.candidate_evaluation import plugin
from proteus.examples.evaluation_gateway import prepare as prepare_gateway, register as register_gateway
from proteus.examples.worker_lifecycle import wait
from proteus.recipes import load_recipe
from proteus.recipes.config import EventPolicySpec


class EventMethod:
    def run(self, services, restored_state):
        initial = services.initial_candidate()
        services.evaluate(initial, "test", operation_id="private-test")
        gateway, events, outputs = services.gateway("formal"), services.events("heartbeat"), {}
        with gateway.serve():
            for name in ("a", "b"):
                services.worker(name).start("search", "Submit a candidate, then wait for a research intervention.")
            decisions = []
            for index in range(2):
                result = gateway.step(f"next-{index}", timeout_seconds=20)
                require(result is not None and result.status == "completed", "expected a formal result")
                ref = services.values.publish("proteus.gateway.result@1", result)
                decisions.append(events.observe(f"observed-{index}", ref))
            for index, decision in enumerate(decisions):
                outputs[f"decision_{index}"] = decision
                dispatches = events.apply(decision)
                require(len(dispatches) == 1, "expected one intervention")
                outputs[f"dispatch_{index}"] = dispatches[0]
                worker_ref = services.values.get(dispatches[0]).worker
                worker = services.worker(worker_ref.worker_id)
                require(wait(worker, worker_ref).state == "completed", "intervention generation did not finish")
                outputs[f"worker_{index}"] = worker.result(worker_ref)
        return MethodResult(outputs=outputs)


def register(registry):
    register_gateway(registry)
    registry.register(Plugin(ref=PluginRef(name="example.event_interventions", version="1"), kind="method",
        options=EmptyOptions, factory=lambda options: EventMethod(), implementation_hash=implementation_hash({
            "method": Path(__file__), "worker": Path(__file__).with_name("_event_worker_fixture.py")}), source="proteus",
        capabilities=("run.journal", "run.resume"), state_schema=EmptyOptions))


def prepare(root):
    path = prepare_gateway(root)
    recipe = load_recipe(path)
    command = (sys.executable, str(Path(__file__).with_name("_event_worker_fixture.py").resolve()))
    options = {**recipe.workers[0].runtime.options, "command": command,
               "command_files": {name: "sha256:" + hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in command}}
    recipe = replace(recipe, name="offline-event-interventions", method=plugin("example.event_interventions"),
        workers=tuple(replace(worker, runtime=replace(worker.runtime, options=options)) for worker in recipe.workers),
        event_policies=(EventPolicySpec(id="heartbeat", gateway="formal", operator=plugin("proteus.event.cadence",
            rules=[{"name": "reflect", "trigger": "evaluations", "every": 1,
                    "prompt": "Reflect on the released formal result and record what to try next."}])),))
    path.write_text(recipe.to_json() + "\n")
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    print(prepare(parser.parse_args().out))


if __name__ == "__main__":
    main()
