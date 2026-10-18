"""Prepare a real Docker/Harbor A/B/A candidate study; no model key is required.

Run with --python /absolute/path/to/pinned-harbor/bin/python --out STUDY,
then lock/run recipe.json using the normal recipe CLI and artifact-store.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from proteus.artifacts.codecs import FileContent, FileDelta, FileWrite
from proteus.artifacts.store import ArtifactStore
from proteus.core.contracts import BudgetLimits, FeedbackVisibility, PluginRef, require
from proteus.core.serialization import content_hash
from proteus.evaluation.tasks import Task, TaskDataset, TaskRecord
from proteus.examples.candidate_evaluation import plugin
from proteus.execution.harbor import identify_runtime
from proteus.methods.compare import CompareOptions, Variant
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.config import (
    EnvironmentSpec, EvaluationSpec, ExecutionSpec, RecipeConfig, ReferenceSpec, SubjectSpec, TaskSuiteSpec,
)


IMAGE = "python@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea"
SOURCE = '''import json, sys
from pathlib import Path
from value import ANSWER
request = json.load(sys.stdin)
assert not Path('/tests/test.sh').exists() and not Path('/solution').exists()
assert ' /logs/verifier ' not in Path('/proc/self/mountinfo').read_text()
# A forged local reward must never become the independent verifier's reward.
Path('/logs/verifier').mkdir(exist_ok=True)
Path('/logs/verifier/reward.txt').write_text('999')
Path('/logs/artifacts/answer.txt').write_text(str(ANSWER))
print(json.dumps({'output': {'answer': ANSWER, 'obsolete': Path('obsolete.txt').exists()},
                  'state': request['state']}))
'''
VERIFIER = '''#!/bin/sh
set -eu
python3 - <<'PY'
from pathlib import Path
assert not Path('/opt/proteus/candidate/main.py').exists()
answer = Path('/logs/artifacts/answer.txt').read_text().strip()
Path('/logs/verifier/reward.txt').write_text(str(int(answer == '42')))
print('PROTEUS_PRIVATE_VERIFIER_23')
PY
'''


def prepare(root: Path, python: str, *, include_timeout=False, runtime=None) -> Path:
    root = Path(root).resolve()
    require(not root.exists() or not any(root.iterdir()), "example output directory already contains data")
    identity = runtime or identify_runtime(python)
    root.mkdir(parents=True, exist_ok=True)
    task = root / "task"
    (task / "tests").mkdir(parents=True)
    (task / "instruction.md").write_text("Write the integer 42 to /logs/artifacts/answer.txt.\n")
    (task / "tests/test.sh").write_text(VERIFIER)
    (task / "task.toml").write_text(f'''schema_version = "1.4"
[environment]
docker_image = "{IMAGE}"
network_mode = "public"
cpus = 1
memory_mb = 512
build_timeout_sec = 120
[agent]
timeout_sec = 3
[verifier]
environment_mode = "separate"
timeout_sec = 30
''')
    store = ArtifactStore(root / "artifact-store", builtin_registry())
    task_ref = store.capture_tree(task)
    initial = store.put(PluginRef(name="proteus.file_tree", version="1"), {
        "main.py": FileContent(SOURCE.encode()), "value.py": FileContent(b"ANSWER = 41\n"),
        "obsolete.txt": FileContent(b"present only in candidate A\n")})
    variants = [Variant(id="improved", delta=FileDelta(base_hash=initial.content_hash,
        put=(FileWrite.from_bytes("value.py", b"ANSWER = 42\n"),), delete=("obsolete.txt",)).to_dict()),
        Variant(id="restored", delta=FileDelta(base_hash=initial.content_hash).to_dict())]
    if include_timeout:
        variants.append(Variant(id="timeout", delta=FileDelta(base_hash=initial.content_hash,
            put=(FileWrite.from_bytes("main.py", b"import time\ntime.sleep(60)\n"),)).to_dict()))
    dataset = TaskDataset(version="harbor-proof-v1", records=(TaskRecord(task=Task(
        id="answer", version="1", split="validation", input={"path": str(task), "tree_hash": task_ref.content_hash}),
        reference=None),))
    data = root / "tasks.json"
    data.write_text(dataset.to_json() + "\n")
    options = CompareOptions(evaluations=("reward",), variants=tuple(variants))
    recipe = RecipeConfig(name="harbor-candidate-proof", method=plugin("proteus.compare", **options.to_dict()),
        actors={}, reference=ReferenceSpec(profile="native"),
        subject=SubjectSpec(runner=plugin("proteus.harbor_subject"), initial_artifact=initial),
        execution=ExecutionSpec(environment=EnvironmentSpec(backend=plugin("proteus.harbor",
            python=str(Path(python).absolute()), runtime_hash=identity["runtime_hash"], bridge_timeout_seconds=240),
            provider="docker", image_digest=IMAGE, network="unrestricted", resources={"cpus": 1, "memory_mb": 512}),
            timeout_seconds=3),
        evaluations=(EvaluationSpec(id="reward", evaluator=plugin("proteus.harbor_reward"),
            tasks=TaskSuiteSpec(suite=plugin("proteus.json_tasks", path=str(data)),
                dataset_version=dataset.version, dataset_hash=content_hash(dataset), split="validation", task_ids=("answer",)),
            purpose="validation", visibility=FeedbackVisibility.OBSERVED, feedback_timing="after_batch"),),
        budget=BudgetLimits(max_evaluations=len(variants) + 1))
    path = root / "recipe.json"
    path.write_text(recipe.to_json() + "\n")
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--python", required=True)
    parser.add_argument("--include-timeout", action="store_true")
    args = parser.parse_args()
    print(prepare(args.out, args.python, include_timeout=args.include_timeout))


if __name__ == "__main__":
    main()
