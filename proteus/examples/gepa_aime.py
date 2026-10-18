"""Prepare, run, resume and audit a GEPA AIME study; no inference during prepare."""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from proteus.artifacts.codecs import TextComponents
from proteus.artifacts.store import ArtifactStore, publish_bytes
from proteus.benchmarks import aime, aime_data, aime_format
from proteus.core.contracts import (
    DIGEST, BudgetLimits, Candidate, EvaluationBatch, FeedbackVisibility, OperationStatus, PluginRef, Rollout, require,
)
from proteus.core.ledger import Ledger, TERMINAL
from proteus.core.methods import MethodResult, Values
from proteus.core.plugins import implementation_hash
from proteus.core.run_lock import run_lock
from proteus.core.serialization import Contract, canonical_json, content_hash, read_json
from proteus.evaluation.records import EvaluationReceipt
from proteus.evaluation.tasks import TaskDataset, make_batch
from proteus.examples.candidate_evaluation import plugin
from proteus.methods.gepa.method import GepaOptions, evaluation_bound, register as register_gepa
from proteus.methods.gepa.reference import REPOSITORY, REVISION
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.cli import installed_packages, source_pin
from proteus.recipes.config import (
    ActorSpec, EnvironmentSpec, EvaluationSpec, ExecutionSpec, RecipeConfig, ReferenceSpec,
    StageSpec, SubjectSpec, TaskSuiteSpec, load_recipe,
)
from proteus.recipes.manifest import DependencyLock, RunManifest
from proteus.recipes.runner import run_recipe


INITIAL_PROMPT = "Solve the math problem carefully. Break down the steps and provide the final answer as a single number."
DEVIATIONS = (
    "Adapted GEPA AIME example, not a claim to reproduce published scores.",
    "Pinned 45/45/30 datasets and DSPy 3.4.1 text-only ChainOfThought format; upstream does not pin these versions.",
    "Managed chat-completions transport; no DSPy JSON fallback, SDK retries, or model-response cache. Malformed fields score zero.",
    "Serial execution and no evaluation cache; original AIME example uses 32 workers and caching. Actual calls are charged.",
    "Single-mutation/no-merge, instance frontier; upstream optimize_anything defaults to hybrid (no objective scores in AIME).",
    "Infrastructure errors stop explicitly; no implicit retry or replacement experiment. Baseline test runs before optimization.",
    "Seed controls GEPA search/task ordering, not provider determinism. Repeated seeds are independent studies.",
)


@dataclass(frozen=True, kw_only=True)
class Settings(Contract):
    solver: ActorSpec
    reflection: ActorSpec
    seeds: tuple[int, ...] = field(default=(0, 1, 2), metadata={"minItems": 1, "uniqueItems": True})
    max_metric_calls: int = field(default=500, metadata={"minimum": 1})
    minibatch_size: int = field(default=3, metadata={"minimum": 1})


@dataclass(frozen=True, kw_only=True)
class StudyRun(Contract):
    seed: int
    arm: Literal["baseline", "optimized"]
    config_hash: str = field(metadata=DIGEST)
    lock_hash: str = field(metadata=DIGEST)

    @property
    def name(self):
        return f"seed-{self.seed}-{self.arm}"


@dataclass(frozen=True, kw_only=True)
class Study(Contract):
    dataset_hash: str = field(metadata=DIGEST)
    provenance_hash: str = field(metadata=DIGEST)
    controller_hash: str = field(metadata=DIGEST)
    settings: Settings
    runs: tuple[StudyRun, ...]

    def _validate(self):
        require(tuple((item.seed, item.arm) for item in self.runs) == tuple(
            (seed, arm) for seed in self.settings.seeds for arm in ("baseline", "optimized")),
            "study must contain exactly both arms for every declared seed")


def registry():
    result = builtin_registry()
    aime.register(result)
    register_gepa(result)
    return result


def controller_hash():
    return implementation_hash({"study": Path(__file__)})


def make_recipes(root, dataset, settings, initial):
    evaluations = tuple(EvaluationSpec(id=split, evaluator=plugin("proteus.aime_metric"),
        tasks=TaskSuiteSpec(suite=plugin("proteus.json_tasks", path=str(root / "tasks.json")),
            dataset_version=dataset.version, dataset_hash=content_hash(dataset), split=split,
            task_ids=tuple(r.task.id for r in dataset.records if r.task.split == split)),
        purpose="optimization" if split == "train" else split, selection_eligible=split != "test",
        visibility=FeedbackVisibility.HIDDEN if split == "test" else FeedbackVisibility.OBSERVED,
        feedback_timing="never" if split == "test" else "after_batch")
        for split in ("train", "validation", "test"))
    options = GepaOptions(train="train", validation="validation", reflection_stage="reflect",
        dataset_stage="side-info", reflection_prompt_template=aime_format.REFLECTION_TEMPLATE,
        max_metric_calls=settings.max_metric_calls, minibatch_size=settings.minibatch_size,
        skip_perfect_score=False, test_evaluations=("test",))
    maximum = evaluation_bound(options, len(evaluations[1].tasks.task_ids)) + len(evaluations[2].tasks.task_ids)
    for seed in settings.seeds:
        for arm in ("baseline", "optimized"):
            baseline = arm == "baseline"
            yield RecipeConfig(name=f"gepa-aime-seed-{seed}-{arm}", seed=seed,
                method=plugin("proteus.compare", evaluations=["test"]) if baseline else plugin("proteus.gepa", **options.to_dict()),
                reference=ReferenceSpec(profile="adapted", code=REPOSITORY, revision=REVISION, deviations=DEVIATIONS),
                actors={"solver": settings.solver, **({} if baseline else {"reflection": settings.reflection})},
                subject=SubjectSpec(runner=plugin("proteus.aime_subject"), initial_artifact=initial, actor="solver"),
                execution=ExecutionSpec(seed=seed, environment=EnvironmentSpec(backend=plugin("proteus.local_json"),
                    provider="host", network="unrestricted")),
                evaluations=(evaluations[2],) if baseline else evaluations,
                stages=() if baseline else (
                    StageSpec(id="reflect", operator=plugin("proteus.prompt"), role="reflection",
                        read_scopes=("proteus.text@1",), write_scopes=("proteus.actor_result@1",)),
                    StageSpec(id="side-info", operator=plugin("proteus.aime_side_info"), role="reflection",
                        read_scopes=("proteus.gepa.reflection_batch@1",), write_scopes=("proteus.gepa.reflection_batch@1",))),
                search={} if baseline else {"parent": plugin("proteus.gepa_parent"),
                    "frontier": plugin("proteus.gepa_frontier"), "final": plugin("proteus.gepa_best")},
                budget=BudgetLimits(max_evaluations=len(evaluations[2].tasks.task_ids) if baseline else maximum,
                    max_model_calls=len(evaluations[2].tasks.task_ids) if baseline else maximum + settings.max_metric_calls))


def prepare(root, dataset, settings, *, sources, packages=(), provenance=None):
    """The Python API also accepts explicitly labelled custom/fixture data."""
    root = Path(root).resolve()
    with run_lock(root):
        require(not root.exists() or not any(root.iterdir()), "study directory already contains data")
        reg = registry()
        store = ArtifactStore(root / "artifact-store", reg)
        initial = store.put(PluginRef(name="proteus.text_components", version="1"),
                            TextComponents(components={"system.txt": INITIAL_PROMPT}))
        publish_bytes(root / "tasks.json", (dataset.to_json() + "\n").encode())
        provenance = provenance if provenance is not None else {"dataset": "custom; not a pinned AIME benchmark"}
        publish_bytes(root / "provenance.json", canonical_json(provenance).encode())
        entries = []
        for recipe in make_recipes(root, dataset, settings, initial):
            recipe = reg.resolve(recipe)
            lock = reg.make_lock(recipe, sources, packages=packages)
            entry = StudyRun(seed=recipe.seed, arm="baseline" if recipe.method.plugin.name == "proteus.compare" else "optimized",
                             config_hash=recipe.config_hash, lock_hash=content_hash(lock))
            publish_bytes(root / "recipes" / (entry.name + ".json"), (recipe.to_json() + "\n").encode())
            publish_bytes(root / "locks" / (entry.name + ".json"), (lock.to_json() + "\n").encode())
            entries.append(entry)
        study = Study(dataset_hash=content_hash(dataset), provenance_hash=content_hash(provenance),
                      controller_hash=controller_hash(), settings=settings, runs=tuple(entries))
        publish_bytes(root / "study.json", (study.to_json() + "\n").encode())
    return root / "study.json"


def load(root, *, check_runtime=True):
    study = Study.from_json((root / "study.json").read_text())
    require(study.controller_hash == controller_hash(), "study controller changed; use the original source")
    dataset = TaskDataset.from_json((root / "tasks.json").read_text())
    require(content_hash(dataset) == study.dataset_hash, "study dataset changed")
    require(content_hash(read_json((root / "provenance.json").read_text())) == study.provenance_hash,
            "study provenance changed")
    reg, runs = registry(), []
    for entry in study.runs:
        recipe = load_recipe(root / "recipes" / (entry.name + ".json"))
        lock = DependencyLock.from_json((root / "locks" / (entry.name + ".json")).read_text())
        require(recipe.config_hash == entry.config_hash and content_hash(lock) == entry.lock_hash,
                "study recipe or lock changed")
        require(recipe.seed == entry.seed, "study seed differs from recipe")
        if check_runtime:
            reg.check_lock(reg.resolve(recipe), lock)
        for spec in recipe.evaluations:
            require(Path(spec.tasks.suite.options["path"]).resolve() == root / "tasks.json", "study dataset path changed")
            make_batch(dataset, spec.tasks)
        runs.append((entry, recipe, lock))
    return study, dataset, reg, runs


def execute(root, *, resume=False):
    root = Path(root).resolve()
    with run_lock(root):
        _, dataset, reg, runs = load(root)
        if not resume:
            require(not (root / "runs").exists(), "study has run data; use resume")
        # Validate every existing child before starting another one.
        for entry, recipe, lock in runs:
            child = root / "runs" / entry.name
            if child.exists():
                manifest = RunManifest.from_json((child / "manifest.json").read_text())
                manifest.assert_resume_compatible(recipe, lock)
        for entry, recipe, lock in runs:
            child = root / "runs" / entry.name
            result = run_recipe(recipe, registry=reg, lock=lock, root=child,
                artifact_source=root / "artifact-store", resume=(child / "manifest.json").exists())
            require(result.status == OperationStatus.COMPLETED,
                    f"{entry.name} stopped: {result.diagnostic}; inspect this run, do not replace it")
            # A comparison can complete with unavailable task scores. Verify its
            # complete evidence before spending another arm's budget.
            _arm_report(child, entry, recipe, lock, dataset, reg)
        return report(root, locked=True)


def _ledger(root, manifest):
    with sqlite3.connect((root / "private/ledger.sqlite3").as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        identities = db.execute("SELECT value FROM identity").fetchall()
        require(len(identities) == 1 and read_json(identities[0][0]) == {"schema_version": 1,
            "run_id": manifest.run_id, "config_hash": manifest.config_hash, "lock_hash": manifest.lock_hash},
            "study ledger identity differs")
        charged, unknown, slots, count = Ledger._balance(db, "run")
        require(slots == 0 and all(row[0] in TERMINAL for row in db.execute("SELECT state FROM entries")),
                "study contains unsettled operations")
        return {"charged": {k: None if value is None else float(value) for k, value in charged.items()},
                "unknown": unknown, "entries": count}


def _arm_report(root, entry, recipe, lock, dataset, reg):
    manifest = RunManifest.from_json((root / "manifest.json").read_text())
    manifest.assert_resume_compatible(recipe, lock)
    require(manifest.run_id == entry.name, "study run identity differs")
    result = MethodResult.from_json((root / "result.json").read_text())
    require(result.status == OperationStatus.COMPLETED, "cannot report an incomplete study run")
    records = read_json(sorted(root.glob("execution-*.json"))[-1].read_text())
    require(records["manifest_hash"] == manifest.manifest_hash and content_hash(records["values"]) == records["values_hash"],
            "study execution identity/values differ")
    values = Values(reg)
    values.restore(records["values"])
    receipt = values.get(result.outputs["initial:test" if entry.arm == "baseline" else "test"])
    require(type(receipt) is EvaluationReceipt and not receipt.batches, "test results must remain hidden from the method")
    candidate_id = "initial" if entry.arm == "baseline" else values.get(result.outputs["candidate"]).id
    candidates = {item["id"]: item for item in records["candidates"]}
    require(candidate_id in candidates, "selected candidate is missing")
    candidate = candidates[candidate_id]
    typed_candidate = Candidate.from_dict(candidate)
    require((typed_candidate.artifact == recipe.subject.initial_artifact) if entry.arm == "baseline"
            else typed_candidate == values.get(result.outputs["candidate"]), "study result candidate differs")
    ArtifactStore(root / "artifacts", reg).get(typed_candidate.artifact)
    test = next(item for item in recipe.evaluations if item.id == "test")
    expected = make_batch(dataset, test.tasks)
    batches = []
    for path in (root / "evaluations").glob("*/protocol.json"):
        protocol = read_json(path.read_text())
        if protocol["evaluation"]["id"] != "test":
            continue
        require(protocol["evaluation"] == test.to_dict() and protocol["batch"] == expected.to_dict()
                and protocol["execution"] == recipe.execution.to_dict(), "test evaluation protocol differs")
        stored_receipt = EvaluationReceipt.from_json((path.parent / "receipt.json").read_text())
        require(stored_receipt == receipt, "test receipt differs from the method result")
        batches.extend(EvaluationBatch.from_json(p.read_text()) for p in sorted((path.parent / "private/batches").glob("*.json")))
    require(len(batches) == 1, "test requires exactly one complete candidate batch")
    batch = batches[0]
    require(batch.run_id == manifest.run_id and batch.candidate_id == candidate_id
            and batch.artifact_hash == candidate["artifact"]["content_hash"] and batch.evaluator == test.evaluator.plugin
            and batch.visibility == FeedbackVisibility.HIDDEN, "test candidate/evaluator identity differs")
    require(batch.protocol_hash == content_hash({"spec": test, "batch": expected,
        "subject": recipe.subject, "execution": recipe.execution, "lock_hash": manifest.lock_hash}), "test protocol hash differs")
    require(tuple(row.task_id for row in batch.rows) == test.tasks.task_ids and batch.expected_rollout_ids == receipt.rollout_ids
            and receipt.candidate_ids == (candidate_id,) * len(expected.tasks), "test coverage/order differs")
    rollouts = {item["id"]: Rollout.from_dict(item) for item in records["rollouts"]}
    references = {r.task.id: r.reference for r in dataset.records if r.task.split == "test"}
    for row in batch.rows:
        require(row.score is not None, "study stopped: test includes unavailable scores")
        rollout = rollouts[row.rollout_id]
        require(rollout.candidate_id == candidate_id and rollout.artifact_hash == batch.artifact_hash
                and rollout.task_id == row.task_id and rollout.split == "test"
                and rollout.run_id == manifest.run_id and rollout.operation_id == batch.operation_id
                and rollout.execution_hash == content_hash(recipe.execution), "test rollout binding differs")
        grade = aime.AimeMetric().grade(None, rollout, references[row.task_id])
        require(row.score == grade.score and row.status == grade.status, "test grade disagrees with the frozen metric")
    score = batch.mean_score()
    require(score is not None, "test includes unavailable scores")
    ledger = _ledger(root, manifest)
    require(ledger["charged"]["evaluations"] == len(rollouts), "study evaluation accounting differs")
    require(ledger["charged"]["model_calls"] == len(records["actor_calls"]), "study actor accounting differs")
    tokens = {key: [call["usage"].get(key) for call in records["actor_calls"]] for key in ("input_tokens", "output_tokens")}
    return {"accuracy": score, "tasks": len(batch.rows), "candidate_id": candidate_id,
        "artifact_hash": batch.artifact_hash, "manifest_hash": manifest.manifest_hash,
        "test_scores": [{"task_id": row.task_id, "score": row.score} for row in batch.rows],
        "ledger": ledger, "tokens": {key: None if None in items else sum(items) for key, items in tokens.items()},
        "metric_calls": None if entry.arm == "baseline" else values.get(result.outputs["summary"]).metric_calls}


def report(root, *, locked=False):
    root = Path(root).resolve()
    if not locked:
        with run_lock(root):
            return report(root, locked=True)
    study, dataset, reg, runs = load(root, check_runtime=False)
    by_seed = {seed: {} for seed in study.settings.seeds}
    for entry, recipe, lock in runs:
        by_seed[entry.seed][entry.arm] = _arm_report(root / "runs" / entry.name, entry, recipe, lock, dataset, reg)
    rows = [{"seed": seed, **arms, "accuracy_delta": arms["optimized"]["accuracy"] - arms["baseline"]["accuracy"]}
            for seed, arms in by_seed.items()]
    metrics = {arm: [row[arm]["accuracy"] for row in rows] for arm in ("baseline", "optimized")}
    metrics["accuracy_delta"] = [row["accuracy_delta"] for row in rows]
    return {"study_hash": content_hash(study), "dataset_hash": study.dataset_hash,
        "dataset_profile": "pinned AIME 45/45/30" if study.dataset_hash == aime_data.DATASET_HASH else "custom/fixture; not AIME reproduction",
        "settings": study.settings.to_dict(), "deviations": list(DEVIATIONS), "seeds": rows,
        "aggregate": {key: {"n": len(items), "mean": statistics.mean(items),
            "sample_sd": statistics.stdev(items) if len(items) > 1 else None} for key, items in metrics.items()},
        "notice": "Completion verifies this declared protocol, not provider determinism or equivalence to published scores. "
                  "Unknown cost/token usage remains unknown. Ledger wall_seconds sums charged operation time, not study elapsed time."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare")
    prep.add_argument("--out", type=Path, required=True)
    prep.add_argument("--data", type=Path, required=True, help="directory prepared by proteus.benchmarks.aime_data")
    prep.add_argument("--source", type=Path, required=True, help="clean Proteus Git checkout")
    prep.add_argument("--settings", type=Path, required=True, help="Settings JSON with solver/reflection ActorSpec; secrets stay in environment")
    for name in ("run", "resume", "report"):
        item = commands.add_parser(name)
        item.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        dataset = TaskDataset.from_json((args.data / "tasks.json").read_text())
        require(content_hash(dataset) == aime_data.DATASET_HASH, "CLI requires the complete pinned AIME dataset")
        source = source_pin("proteus", args.source)
        require(source.dirty_patch_hash is None, "freeze a clean source commit before preparing a real study")
        print(prepare(args.out, dataset, Settings.from_json(args.settings.read_text()), sources=[source],
            packages=installed_packages(), provenance=read_json((args.data / "provenance.json").read_text())))
    else:
        result = report(args.out) if args.command == "report" else execute(args.out, resume=args.command == "resume")
        print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2))


if __name__ == "__main__":
    main()
