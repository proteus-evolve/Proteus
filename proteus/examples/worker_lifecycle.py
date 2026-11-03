"""Two persistent subprocess workers, cooperative pause and exact session continuation."""

import argparse
import hashlib
from pathlib import Path
import sys
import time

from proteus.core.contracts import BudgetLimits, PluginRef, require
from proteus.core.methods import MethodResult
from proteus.core.plugins import EmptyOptions, Plugin, implementation_hash
from proteus.examples.candidate_evaluation import plugin
from proteus.recipes.config import ActorSpec, RecipeConfig, ReferenceSpec, WorkerSpec
from proteus.workers.records import CommandWorkerOptions


def wait(worker, ref, *, ready=False):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        observed = worker.poll(ref)
        if observed.completion is not None:
            return observed
        if ready and (worker.controller.workspace / ("ready-" + ref.operation_id)).exists():
            return observed
        time.sleep(0.025)
    worker.control(ref, "stop")
    raise TimeoutError("offline worker did not reach the expected state")


class WorkerMethod:
    def run(self, services, restored_state):
        a, b = services.worker("a"), services.worker("b")
        first = a.start("first", "wait for intervention")
        other = b.start("first", "complete the offline work")
        wait(a, first, ready=True)
        a.control(first, "pause")
        require(wait(a, first).state == "paused", "worker A did not pause")
        require(wait(b, other).state == "completed", "worker B did not complete")
        resumed = a.start("second", "continue the saved session", previous=first)
        require(wait(a, resumed).state == "completed", "worker A did not resume")
        return MethodResult(outputs={"a_paused": a.result(first), "a_resumed": a.result(resumed),
                                     "b_completed": b.result(other)})


def register(registry):
    digest = implementation_hash({"worker_lifecycle.py": Path(__file__),
                                  "fixture.py": Path(__file__).with_name("_worker_protocol_fixture.py")})
    registry.register(Plugin(ref=PluginRef(name="example.worker_lifecycle", version="1"), kind="method",
        options=EmptyOptions, factory=lambda options: WorkerMethod(), implementation_hash=digest, source="proteus",
        capabilities=("run.journal", "run.resume"), state_schema=EmptyOptions))


def prepare(root):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    command = (sys.executable, str(Path(__file__).with_name("_worker_protocol_fixture.py").resolve()))
    options = CommandWorkerOptions(command=command,
        command_files={path: "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in command},
        timeout_seconds=30, shutdown_grace_seconds=0.2)
    recipe = RecipeConfig(name="offline-worker-lifecycle", method=plugin("example.worker_lifecycle"),
        reference=ReferenceSpec(profile="native"),
        actors={"researcher": ActorSpec(runtime=plugin("proteus.echo"), budget=BudgetLimits(max_concurrency=2))},
        workers=tuple(WorkerSpec(id=name, role="researcher", runtime=plugin("proteus.worker.command", **options.to_dict()))
                      for name in ("a", "b")), budget=BudgetLimits(max_concurrency=2))
    path = root / "recipe.json"
    path.write_text(recipe.to_json() + "\n")
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    print(prepare(parser.parse_args().out))


if __name__ == "__main__":
    main()
