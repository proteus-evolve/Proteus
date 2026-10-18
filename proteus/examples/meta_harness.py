"""Prepare the pinned three-domain Meta-Harness recipe with explicit live runtimes."""

import argparse
from pathlib import Path

from proteus.artifacts.codecs import FileContent
from proteus.artifacts.store import ArtifactStore
from proteus.core.contracts import FeedbackVisibility, PluginRef, StatePolicy, require
from proteus.core.serialization import canonical_json, content_hash
from proteus.evidence.records import EvidenceAccess
from proteus.examples.candidate_evaluation import plugin
from proteus.examples.harbor_evaluation import IMAGE
from proteus.execution.harbor import identify_runtime
from proteus.methods.meta_harness import entry, reference
from proteus.methods.meta_harness.data import DIRECTORIES, dataset, save_provenance
from proteus.methods.meta_harness.records import REPOSITORY, REVISION, DatasetProfile, MetaOptions
from proteus.recipes.builtins import builtin_registry
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
)


DEVIATIONS = (
    "Serial fresh processes carry memory through get_state/set_state; nonserialized process globals and thread scheduling are not preserved.",
    "Model transport uses managed fresh calls via the upstream LLMCallable protocol instead of the original provider SDK.",
    "The proposer uses the explicitly configured workspace runtime; Claude Code, its model, and its latency/cost are not implied.",
    "The original proposer text is retained with explicit read-only input/history and output directory adaptations.",
    "Candidate names must be unique; failed/incomplete dataset groups cannot enter selection; exact metric ties use stable system-name order.",
    "Uncertain external calls stop for reconciliation; no automatic retry or imputed model response is used.",
    "Proposal manifests require complete typed metadata and unique names; a malformed manifest rejects that whole proposal batch.",
    "Candidate RNG is initialized for each managed call; state outside get_state/set_state, including RNG state, is not carried implicitly.",
)


def reference_tree():
    root = Path(reference.__file__).parent
    names = (
        "__init__.py",
        "memory_system.py",
        "llm.py",
        "agents/__init__.py",
        "agents/no_memory.py",
        "agents/fewshot_memory.py",
        "agents/fewshot_all.py",
    )
    files = {
        "text_classification/" + name: FileContent((root / name).read_bytes()) for name in names
    }
    files["candidate.py"] = FileContent(Path(entry.__file__).read_bytes())
    files["selected.json"] = FileContent(canonical_json({"system": "no_memory"}).encode())
    files["LICENSE-META-HARNESS.txt"] = FileContent(
        (root.parent / "LICENSE-META-HARNESS.txt").read_bytes()
    )
    return files


def build_recipe(
    root,
    pools,
    model,
    proposer,
    execution,
    *,
    iterations=1,
    mode="offline",
    num_epochs=1,
    batch_size=1,
):
    """Shared assembly for pinned real data and explicitly named offline fixtures."""
    root = Path(root).resolve()
    require(not root.exists() or not any(root.iterdir()), "recipe output directory must be empty")
    root.mkdir(parents=True, exist_ok=True)
    evaluations, profiles = [], []
    for name, seed, data, orders in pools:
        path = root / (name + "-" + str(seed) + ".json")
        path.write_text(data.to_json() + "\n")
        ids = {}
        for split, purpose in (("train", "optimization"), ("val", "validation"), ("test", "test")):
            key = name + "-" + str(seed) + "-" + split
            ids[split] = key
            tasks = TaskSuiteSpec(
                suite=plugin("proteus.json_tasks", path=str(path)),
                dataset_version=data.version,
                dataset_hash=content_hash(data),
                split=split,
                task_ids=orders[split],
                seed=seed,
            )
            evaluations.append(
                EvaluationSpec(
                    id=key,
                    evaluator=plugin("proteus.meta_grade", dataset=name),
                    tasks=tasks,
                    purpose=purpose,
                    visibility=FeedbackVisibility.HIDDEN
                    if split == "test"
                    else FeedbackVisibility.OBSERVED,
                    feedback_timing={
                        "train": "after_prediction",
                        "val": "after_batch",
                        "test": "never",
                    }[split],
                    selection_eligible=split != "test",
                    allow_supervision=split == "train" and mode == "offline",
                )
            )
        profiles.append(
            DatasetProfile(
                name=name, seed=seed, train=ids["train"], validation=ids["val"], test=ids["test"]
            )
        )
    options = MetaOptions(
        datasets=tuple(profiles),
        iterations=iterations,
        mode=mode,
        num_epochs=num_epochs,
        batch_size=batch_size,
    )
    registry = builtin_registry()
    artifact = ArtifactStore(root / "artifact-store", registry).put(
        PluginRef(name="proteus.file_tree", version="1"), reference_tree()
    )
    public = tuple(e for e in evaluations if e.visibility == FeedbackVisibility.OBSERVED)
    stage = StageSpec(
        id="propose",
        operator=plugin("proteus.workspace"),
        role="proposer",
        failure_policy="record",
        input_types={
            "prompt": "proteus.text@1",
            "seed": "proteus.artifact_ref@1",
            "history": "proteus.evidence.view@1",
        },
        output_types={"response": "proteus.actor_result@1"},
        read_scopes=("proteus.text@1", "proteus.artifact_ref@1", "proteus.evidence.view@1"),
        write_scopes=("proteus.actor_result@1",),
        evidence=EvidenceAccess(
            evaluation_ids=tuple(e.id for e in public),
            splits=tuple(sorted({e.tasks.split for e in public})),
        ),
    )
    recipe = RecipeConfig(
        name="meta-harness-" + mode,
        method=plugin("proteus.meta_harness", **options.to_dict()),
        actors={"model": model, "proposer": proposer},
        stages=(stage,),
        evaluations=tuple(evaluations),
        subject=SubjectSpec(
            runner=plugin(
                "proteus.code_subject",
                class_name="Candidate",
                methods=["inspect", "predict", "learn_from_batch"],
            ),
            initial_artifact=artifact,
            actor="model",
            input_types={"memory": "proteus.code.state@1"},
        ),
        execution=execution,
        reference=ReferenceSpec(
            profile="adapted",
            code=REPOSITORY,
            revision=REVISION,
            paper="https://arxiv.org/abs/2603.28052",
            deviations=DEVIATIONS,
        ),
    )
    (root / "recipe.json").write_text(recipe.to_json() + "\n")
    return root / "recipe.json"


def prepare(
    root,
    source,
    model,
    proposer,
    harbor_python,
    *,
    datasets=tuple(DIRECTORIES),
    seeds=(42,),
    iterations=1,
    mode="offline",
    num_epochs=1,
    batch_size=1,
):
    runtime = identify_runtime(harbor_python)
    execution = ExecutionSpec(
        environment=EnvironmentSpec(
            backend=plugin(
                "proteus.harbor_code", python=harbor_python, runtime_hash=runtime["runtime_hash"]
            ),
            provider="docker",
            image_digest=IMAGE,
            resources={"cpus": 1, "memory_mb": 512},
            network="none",
            lifecycle="sequence",
        ),
        state_policy=StatePolicy.SEQUENCE,
        timeout_seconds=180,
    )
    pools, sources = [], {}
    require(
        len(set(datasets)) == len(datasets) and len(set(seeds)) == len(seeds),
        "duplicate dataset or seed",
    )
    for name in datasets:
        for seed in seeds:
            data, order, provenance = dataset(source, name, seed)
            pools.append((name, seed, data, order))
            sources.update(provenance)
    path = build_recipe(
        root,
        pools,
        model,
        proposer,
        execution,
        iterations=iterations,
        mode=mode,
        num_epochs=num_epochs,
        batch_size=batch_size,
    )
    save_provenance(path.parent / "data-provenance.json", sources)
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--reference-source", type=Path, required=True)
    parser.add_argument(
        "--model-config",
        type=Path,
        required=True,
        help="ActorSpec JSON for the managed prediction model",
    )
    parser.add_argument(
        "--proposer-config",
        type=Path,
        required=True,
        help="ActorSpec JSON for the workspace coding runtime",
    )
    parser.add_argument("--harbor-python", required=True)
    parser.add_argument(
        "--datasets", choices=tuple(DIRECTORIES), nargs="+", default=list(DIRECTORIES)
    )
    parser.add_argument("--seeds", type=int, nargs="+", default=[42])
    parser.add_argument("--iterations", type=int, default=1)
    parser.add_argument("--mode", choices=("offline", "online"), default="offline")
    parser.add_argument("--num-epochs", type=int, default=1)
    parser.add_argument("--batch-size", type=int, default=1)
    args = parser.parse_args()
    print(
        prepare(
            args.out,
            args.reference_source,
            ActorSpec.from_json(args.model_config.read_text()),
            ActorSpec.from_json(args.proposer_config.read_text()),
            args.harbor_python,
            datasets=tuple(args.datasets),
            seeds=tuple(args.seeds),
            iterations=args.iterations,
            mode=args.mode,
            num_epochs=args.num_epochs,
            batch_size=args.batch_size,
        )
    )


if __name__ == "__main__":
    main()
