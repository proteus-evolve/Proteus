"""Prepare an offline code-candidate comparison, including a held-out private split.

Run ``python -m proteus.examples.candidate_evaluation --out /tmp/candidate-study``.
Then lock/run its recipe with the normal recipe CLI; no model credentials are needed.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from proteus.artifacts.codecs import FileContent, FileDelta, FileWrite
from proteus.artifacts.store import ArtifactStore
from proteus.core.contracts import BudgetLimits, FeedbackVisibility, PluginRef, require
from proteus.core.serialization import content_hash
from proteus.evaluation.tasks import Task, TaskDataset, TaskRecord
from proteus.methods.compare import CompareOptions, Variant
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.config import (
    EnvironmentSpec, EvaluationSpec, ExecutionSpec, PluginSpec, RecipeConfig, ReferenceSpec,
    SubjectSpec, TaskSuiteSpec,
)


def plugin(name, **options):
    return PluginSpec(plugin=PluginRef(name=name, version="1"), options=options)


def prepare(root: Path) -> Path:
    root = Path(root).resolve()
    require(not root.exists() or not any(root.iterdir()), "example output directory already contains data")
    root.mkdir(parents=True, exist_ok=True)
    dataset = TaskDataset(version="offline-double-v1", records=tuple(
        TaskRecord(task=Task(id=name, version="1", split=split, input=value), reference=value * 2)
        for name, split, value in (("two", "validation", 2), ("three", "validation", 3), ("seven", "test", 7))))
    data_path = root / "tasks.json"
    data_path.write_text(dataset.to_json() + "\n", encoding="utf-8")
    source = "import json, sys\nr = json.load(sys.stdin)\nprint(json.dumps({'output': r['task']['input'] * FACTOR, 'state': r['state']}))\n"
    store = ArtifactStore(root / "artifact-store", builtin_registry())
    initial = store.put(PluginRef(name="proteus.file_tree", version="1"),
                        {"main.py": FileContent(source.replace("FACTOR", "1").encode())})
    delta = FileDelta(base_hash=initial.content_hash,
                      put=(FileWrite.from_bytes("main.py", source.replace("FACTOR", "2").encode()),))

    def tasks(split, ids):
        return TaskSuiteSpec(suite=plugin("proteus.json_tasks", path=str(data_path)),
            dataset_version=dataset.version, dataset_hash=content_hash(dataset), split=split, task_ids=ids)

    options = CompareOptions(evaluations=("validation", "test"), variants=(Variant(id="doubled", delta=delta.to_dict()),))
    recipe = RecipeConfig(name="offline-candidate-comparison", method=plugin("proteus.compare", **options.to_dict()),
        actors={}, reference=ReferenceSpec(profile="native"),
        subject=SubjectSpec(runner=plugin("proteus.python_subject"), initial_artifact=initial),
        execution=ExecutionSpec(environment=EnvironmentSpec(backend=plugin("proteus.local_json"),
            provider="host", network="unrestricted"), timeout_seconds=10),
        evaluations=(EvaluationSpec(id="validation", evaluator=plugin("proteus.exact_match"),
            tasks=tasks("validation", ("two", "three")), purpose="validation",
            visibility=FeedbackVisibility.OBSERVED, feedback_timing="after_batch"),
            EvaluationSpec(id="test", evaluator=plugin("proteus.exact_match"),
                tasks=tasks("test", ("seven",)), purpose="test", selection_eligible=False)),
        budget=BudgetLimits(max_evaluations=6))
    path = root / "recipe.json"
    path.write_text(recipe.to_json() + "\n", encoding="utf-8")
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    print(prepare(args.out))


if __name__ == "__main__":
    main()
