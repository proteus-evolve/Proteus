"""Real candidate source-build/cache A/B/A study on the pinned Harbor backend."""

from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path

from proteus.artifacts.codecs import FileContent, FileDelta, FileWrite
from proteus.artifacts.store import ArtifactStore
from proteus.core.contracts import BudgetLimits, PluginRef
from proteus.core.serialization import content_hash
from proteus.evaluation.tasks import TaskDataset
from proteus.examples.candidate_evaluation import plugin
from proteus.examples.harbor_evaluation import SOURCE, prepare as prepare_base
from proteus.execution.builds import SourceBuildSpec
from proteus.methods.compare import CompareOptions, Variant
from proteus.recipes import load_recipe
from proteus.recipes.builtins import builtin_registry


BUILD = '''import json
from pathlib import Path
inputs = list(Path('.').glob('data-*.json'))
assert len(inputs) == 1
answer = json.loads(inputs[0].read_text())['answer']
Path('value.py').write_text('ANSWER = ' + str(answer) + '\\n')
Path('main.py').write_text(Path('main.py.template').read_text())
# This build-stage mutation must not escape into the fresh runtime/verifier.
Path('/build-stage-only').write_text('private builder state')
'''


def prepare(root: Path, python: str, *, runtime=None) -> Path:
    path = prepare_base(root, python, runtime=runtime)
    root = path.parent
    recipe = load_recipe(path)
    store = ArtifactStore(root / "artifact-store", builtin_registry())
    initial = store.put(PluginRef(name="proteus.file_tree", version="1"), {
        "build.py": FileContent(BUILD.encode()), "main.py.template": FileContent(SOURCE.replace(
            "request = json.load(sys.stdin)", "request = json.load(sys.stdin)\nassert not Path('/build-stage-only').exists()").encode()),
        "data-original.json": FileContent(b'{"answer":41}'), "obsolete.txt": FileContent(b"candidate A only"),
    })
    variants = (
        Variant(id="improved", delta=FileDelta(base_hash=initial.content_hash,
            put=(FileWrite.from_bytes("data-renamed.json", b'{"answer":42}'),),
            delete=("data-original.json", "obsolete.txt")).to_dict()),
        Variant(id="restored", delta=FileDelta(base_hash=initial.content_hash).to_dict()),
        Variant(id="broken", delta=FileDelta(base_hash=initial.content_hash,
            put=(FileWrite.from_bytes("build.py", b"raise RuntimeError('intentional build failure')\n"),)).to_dict()),
    )
    verifier = root / "task/tests/test.sh"
    verifier.write_text(verifier.read_text().replace("answer = Path", "assert not Path('/opt/proteus/build').exists()\n"
        "assert not Path('/opt/proteus/source').exists()\nassert not Path('/build-stage-only').exists()\nanswer = Path"))
    task_ref = store.capture_tree(root / "task")
    dataset = TaskDataset.from_json((root / "tasks.json").read_text())
    dataset = replace(dataset, records=tuple(replace(record, task=replace(record.task,
        input={"path": str(root / "task"), "tree_hash": task_ref.content_hash})) for record in dataset.records))
    (root / "tasks.json").write_text(dataset.to_json() + "\n")
    spec = SourceBuildSpec(commands=("python3 build.py",))
    environment = recipe.execution.environment
    recipe = replace(recipe, name="harbor-source-build-proof",
        method=plugin("proteus.compare", **CompareOptions(evaluations=("reward",), variants=variants).to_dict()),
        subject=replace(recipe.subject, initial_artifact=initial),
        execution=replace(recipe.execution, environment=replace(environment, build_hash=content_hash(spec),
            backend=replace(environment.backend, options={**environment.backend.options, "source_build": spec.to_dict()}))),
        evaluations=(replace(recipe.evaluations[0], tasks=replace(recipe.evaluations[0].tasks,
            dataset_hash=content_hash(dataset))),), budget=BudgetLimits(max_evaluations=4))
    path.write_text(recipe.to_json() + "\n")
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--python", required=True)
    args = parser.parse_args()
    print(prepare(args.out, args.python))


if __name__ == "__main__":
    main()
