"""Model-free adaptive workflow fixture: independent candidates, common budgets."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path

from proteus.artifacts.codecs import FileContent, FileDelta, FileWrite
from proteus.artifacts.store import ArtifactStore
from proteus.core.contracts import (
    BudgetLimits,
    Candidate,
    FeedbackVisibility,
    OperationStatus,
    Outcome,
    PluginRef,
    require,
)
from proteus.core.methods import MethodResult
from proteus.core.plugins import EmptyOptions, Plugin, implementation_hash
from proteus.core.serialization import Contract, content_hash
from proteus.core.workflows import WorkflowJob
from proteus.evaluation.tasks import Task, TaskDataset, TaskRecord, TaskSelection
from proteus.examples.candidate_evaluation import plugin
from proteus.recipes.builtins import Text, builtin_registry
from proteus.recipes.config import (
    ActorSpec,
    EnvironmentSpec,
    EvaluationSpec,
    ExecutionSpec,
    RecipeConfig,
    ReferenceSpec,
    StageSpec,
    SubjectSpec,
    TaskSuiteSpec,
    WorkflowSpec,
)


@dataclass(frozen=True, kw_only=True)
class SampleInput(Contract):
    candidate: Candidate
    task_id: str


@dataclass(frozen=True, kw_only=True)
class Options(Contract):
    workers: int = field(default=3, metadata={"minimum": 1})


class RefineSample:
    def run(self, services, inputs):
        item = inputs["sample"]
        current = item.candidate
        for _ in range(4):
            session = services.open_evaluation(
                "train", selection=TaskSelection(task_ids=(item.task_id,))
            )
            observation = session.step(current)
            session.finish()
            require(
                observation.rollout.status == Outcome.SUCCEEDED
                and observation.row is not None
                and observation.row.score in (0, 1),
                "fixture prediction or grade unavailable",
            )
            if observation.row.score == 1:
                return {"candidate": current}
            # Echo returns an explicitly deterministic proposal. This fixture
            # exercises orchestration; it makes no claims about learned quality.
            prompt = services.values.publish(
                "proteus.text@1", Text(text=str(observation.rollout.output + 1))
            )
            proposal = services.stage("propose", {"prompt": prompt})
            text = services.values.get(proposal.outputs["response"]).text
            current = services.fork_candidate(
                current,
                delta=FileDelta(
                    base_hash=current.artifact.content_hash,
                    put=(FileWrite.from_bytes("value.txt", text.encode()),),
                ),
            )
        raise ValueError("fixture sample did not reach its target")


class Study:
    def __init__(self, options):
        self.options = options

    def run(self, services, restored_state):
        initial = services.initial_candidate()
        tasks = services.task_pool("train")
        jobs = tuple(
            WorkflowJob(
                key=task.id,
                workflow="refine",
                inputs={
                    "sample": services.values.publish(
                        "example.workflow_sample@1", SampleInput(candidate=initial, task_id=task.id)
                    )
                },
            )
            for task in tasks.tasks
        )
        mapped = services.map_workflows(
            jobs, operation_id="refine-samples", max_workers=self.options.workers
        )
        return MethodResult(
            status=OperationStatus.COMPLETED if mapped.succeeded else OperationStatus.FAILED,
            outputs={"workflows": services.values.publish("proteus.workflow_map@1", mapped)},
            diagnostic="" if mapped.succeeded else "a sample workflow failed; inspect its receipt",
        )


def register(registry):
    registry.register_data("example.workflow_sample@1", SampleInput)
    digest = implementation_hash({"adaptive_workflows.py": Path(__file__)})
    registry.register(
        Plugin(
            ref=PluginRef(name="example.refine_sample", version="1"),
            kind="workflow",
            options=EmptyOptions,
            factory=lambda options: RefineSample(),
            source="proteus",
            implementation_hash=digest,
            capabilities=("workflow.parallel", "workflow.replay"),
            inputs={"sample": "example.workflow_sample@1"},
            outputs={"candidate": "proteus.candidate@1"},
        )
    )
    registry.register(
        Plugin(
            ref=PluginRef(name="example.adaptive_workflows", version="1"),
            kind="method",
            options=Options,
            factory=Study,
            source="proteus",
            implementation_hash=digest,
            capabilities=("run.journal", "run.resume"),
            state_schema=EmptyOptions,
        )
    )


def prepare(root, *, workers=3):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    registry = builtin_registry()
    store = ArtifactStore(root / "artifact-store", registry)
    source = (
        "import json,sys\nfrom pathlib import Path\nr=json.load(sys.stdin)\n"
        f'with Path({str(root / "fixture-invocations.txt")!r}).open("a") as stream: stream.write(r["task"]["id"]+"\\n")\n'
        'print(json.dumps({"output":int(Path("value.txt").read_text())}))\n'
    )
    artifact = store.put(
        PluginRef(name="proteus.file_tree", version="1"),
        {"main.py": FileContent(source.encode()), "value.txt": FileContent(b"0")},
    )
    dataset = TaskDataset(
        version="workflow-fixture-v1",
        records=tuple(
            TaskRecord(task=Task(id=str(i), version="1", split="train", input=i), reference=i + 1)
            for i in range(3)
        ),
    )
    (root / "tasks.json").write_text(dataset.to_json() + "\n")
    recipe = RecipeConfig(
        name="offline-adaptive-workflow-fixture",
        method=plugin("example.adaptive_workflows", workers=workers),
        reference=ReferenceSpec(profile="native"),
        actors={
            "optimizer": ActorSpec(
                runtime=plugin("proteus.echo"), budget=BudgetLimits(max_concurrency=2)
            )
        },
        subject=SubjectSpec(runner=plugin("proteus.python_subject"), initial_artifact=artifact),
        execution=ExecutionSpec(
            environment=EnvironmentSpec(
                backend=plugin("proteus.local_json"), provider="host", network="unrestricted"
            ),
            timeout_seconds=3,
        ),
        stages=(
            StageSpec(
                id="propose",
                operator=plugin("proteus.prompt"),
                role="optimizer",
                read_scopes=("proteus.text@1",),
                write_scopes=("proteus.actor_result@1",),
            ),
        ),
        workflows=(
            WorkflowSpec(
                id="refine",
                operator=plugin("example.refine_sample"),
                stages=("propose",),
                evaluations=("train",),
            ),
        ),
        evaluations=(
            EvaluationSpec(
                id="train",
                evaluator=plugin("proteus.exact_match"),
                visibility=FeedbackVisibility.OBSERVED,
                feedback_timing="after_prediction",
                tasks=TaskSuiteSpec(
                    suite=plugin("proteus.json_tasks", path=str(root / "tasks.json")),
                    dataset_version=dataset.version,
                    dataset_hash=content_hash(dataset),
                    split="train",
                    task_ids=("0", "1", "2"),
                ),
            ),
        ),
        budget=BudgetLimits(max_evaluations=9, max_concurrency=3),
    )
    path = root / "recipe.json"
    path.write_text(recipe.to_json() + "\n")
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args()
    print(prepare(args.out, workers=args.workers))


if __name__ == "__main__":
    main()
