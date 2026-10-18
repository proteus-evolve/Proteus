"""Prepare an offline cooperative CORAL controller study; no model calls are made."""

import argparse
from dataclasses import replace
import hashlib
from pathlib import Path
import sys

from proteus.core.contracts import BudgetLimits
from proteus.examples.candidate_evaluation import plugin
from proteus.examples.evaluation_gateway import prepare as prepare_gateway, register as register_gateway
from proteus.methods.coral.method import register as register_method
from proteus.methods.coral.policy import register as register_policy
from proteus.recipes import load_recipe
from proteus.recipes.config import EventPolicySpec, ReferenceSpec


def register(registry):
    register_gateway(registry)
    register_policy(registry)
    register_method(registry)


def prepare(root, *, workers=2):
    from proteus.core.contracts import require
    require(workers in (1, 2), "the offline fixture supports one or two workers")
    path = prepare_gateway(root)
    recipe = load_recipe(path)
    command = (sys.executable, str(Path(__file__).with_name("_coral_worker_fixture.py").resolve()))
    runtime = replace(recipe.workers[0].runtime, options={**recipe.workers[0].runtime.options,
        "command": command, "timeout_seconds": 90, "shutdown_grace_seconds": 2,
        "command_files": {name: "sha256:" + hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in command}})
    names = ("a", "b")[:workers]
    recipe = replace(recipe, name="offline-coral-search", method=plugin("coral.single_island",
        gateway="formal", event_policy="heartbeat", max_submissions=workers + 1, max_polls=300,
        poll_seconds=.1, final_evaluation="test",
        bootstrap_prompt="Explore the editable candidate/ directory, submit exact files to the formal gateway, "
            "and share sourced findings through its notes and skills API."),
        reference=ReferenceSpec(profile="adapted", status="experimental",
            code="https://github.com/Human-Agent-Society/CORAL",
            revision="0123dfb939b35228cf2c1fde224cd0561e727408",
            deviations=("Offline deterministic worker fixture, not a model benchmark.",
                "Serial formal dispatch observes every result; the reference manager coalesces observations.",
                "Explicit submission, poll and restart bounds; no multi-island coordination.",
                "Proteus-mediated knowledge and submissions; adapted heartbeat prompts.")),
        workers=tuple(replace(worker, runtime=runtime) for worker in recipe.workers if worker.id in names),
        gateways=(replace(recipe.gateways[0], members={name: "inspect" for name in names},
            knowledge_access="read_write", max_evaluations_per_worker=4),),
        search={"final": plugin("proteus.scalar_best")},
        event_policies=(EventPolicySpec(id="heartbeat", gateway="formal", operator=plugin("coral.heartbeat",
            rules=[{"name": "reflect", "trigger": "evaluations", "every": 1,
                "prompt": "Inspect your released formal feedback. Record useful findings in shared notes or skills, "
                    "then continue improving candidate/ and submitting exact snapshots."}])),),
        budget=BudgetLimits(max_evaluations=2 + 2 * (workers + 1) + 1, max_concurrency=workers + 1))
    path.write_text(recipe.to_json() + "\n")
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--workers", type=int, choices=(1, 2), default=2)
    args = parser.parse_args()
    print(prepare(args.out, workers=args.workers))


if __name__ == "__main__":
    main()
