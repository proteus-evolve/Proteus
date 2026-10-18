"""Prepare, run, resume and audit ACE on the pinned Formula dataset.

Preparation performs no inference. Offline measures the best validation playbook
on held-out test tasks; online measures windows before learning from those tasks.
"""

from __future__ import annotations

import argparse
import json
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from proteus.artifacts.store import ArtifactStore, publish_bytes
from proteus.benchmarks import ace_formula as domain, ace_formula_data as data
from proteus.core.contracts import DIGEST, BudgetLimits, FeedbackVisibility, OperationStatus, SessionPolicy, require
from proteus.core.plugins import implementation_hash
from proteus.core.run_lock import run_lock
from proteus.core.serialization import Contract, canonical_json, content_hash, read_json
from proteus.evaluation import audit as audit_module
from proteus.evaluation.tasks import TaskDataset, make_batch
from proteus.examples import ace_formula_audit
from proteus.examples.candidate_evaluation import plugin
from proteus.methods.ace.method import AceOptions, register as register_ace
from proteus.methods.ace.playbook import CODEC, REPOSITORY, REVISION, Playbook
from proteus.methods.ace.roles import AceTask
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.cli import installed_packages, source_pin
from proteus.recipes.config import (
    ActorSpec, EnvironmentSpec, EvaluationSpec, ExecutionSpec, RecipeConfig, ReferenceSpec,
    StageSpec, SubjectSpec, TaskSuiteSpec, load_recipe,
)
from proteus.recipes.manifest import DependencyLock, RunManifest
from proteus.recipes.runner import run_recipe


DEVIATIONS = (
    "Adapted ACE Formula recipe, not a reproduction of published scores or all ACE domains.",
    "The published-data profile uses pinned upstream 500/300/200 files in original order; custom data is labelled separately. "
    "Both use the reference parser/metric contract with exact numeric/string correctness.",
    "Managed chat-completions transport and serial trials; no upstream SDK retries or incorrect-answer fallback on transport failure.",
    "Independent role model/settings are explicit; no automatic selection of the upstream DeepSeek/SambaNova service.",
    "No optional embedding analyzer. The 80000-token playbook budget is a prompt hint, as in the reference core.",
    "Online uses the official test pool for pre-learning window scores and subsequent learning; this is an adaptive-stream metric.",
    "Seeds label independent replicates; ACE's declared order is fixed and provider determinism is not assumed.",
)


@dataclass(frozen=True, kw_only=True)
class Settings(Contract):
    generator: ActorSpec
    reflector: ActorSpec
    curator: ActorSpec
    seeds: tuple[int, ...] = field(default=(0, 1, 2), metadata={"minItems": 1, "uniqueItems": True})
    modes: tuple[Literal["offline", "online"], ...] = field(default=("offline", "online"),
        metadata={"minItems": 1, "uniqueItems": True})
    num_epochs: int = field(default=1, metadata={"minimum": 1})
    max_num_rounds: int = field(default=3, metadata={"minimum": 0})
    curator_frequency: int = field(default=1, metadata={"minimum": 1})
    eval_steps: int = field(default=100, metadata={"minimum": 1})
    online_eval_frequency: int = field(default=15, metadata={"minimum": 1})
    token_budget: int = field(default=80000, metadata={"minimum": 1})
    no_ground_truth: bool = False
    json_mode: bool = False

    def _validate(self):
        for actor in (self.generator, self.reflector, self.curator):
            require(actor.runtime.plugin.name == "proteus.chat" and actor.session == SessionPolicy.FRESH
                    and not actor.tools and actor.prompt_hash is None,
                    "Formula study requires fresh chat actors without tools or extra static prompts")


@dataclass(frozen=True, kw_only=True)
class StudyRun(Contract):
    seed: int
    mode: Literal["offline", "online"]
    config_hash: str = field(metadata=DIGEST)
    lock_hash: str = field(metadata=DIGEST)

    @property
    def name(self):
        return f"seed-{self.seed}-{self.mode}"


@dataclass(frozen=True, kw_only=True)
class Study(Contract):
    dataset_hash: str = field(metadata=DIGEST)
    provenance_hash: str = field(metadata=DIGEST)
    controller_hash: str = field(metadata=DIGEST)
    settings: Settings
    runs: tuple[StudyRun, ...]

    def _validate(self):
        require(tuple((item.seed, item.mode) for item in self.runs) == tuple(
            (seed, mode) for seed in self.settings.seeds for mode in self.settings.modes),
            "study must contain every declared seed/mode exactly once in order")


def registry():
    reg = builtin_registry()
    register_ace(reg)
    domain.register(reg)
    return reg


def controller_hash():
    return implementation_hash({"study": Path(__file__), "report": Path(ace_formula_audit.__file__),
                                "audit": Path(audit_module.__file__)})


def make_recipes(root, dataset, settings, initial):
    require({item.task.split for item in dataset.records} == {"train", "validation", "test"},
            "Formula study requires exactly train, validation and test splits")
    for item in dataset.records:
        AceTask.from_dict(item.task.input)
        require(type(item.reference) is str, "Formula targets must be strings")

    def evaluation(key, split, purpose):
        return EvaluationSpec(id=key, evaluator=plugin("proteus.ace_formula_metric"),
            tasks=TaskSuiteSpec(suite=plugin("proteus.json_tasks", path=str(root / "tasks.json")),
                dataset_version=dataset.version, dataset_hash=content_hash(dataset), split=split,
                task_ids=tuple(item.task.id for item in dataset.records if item.task.split == split)),
            purpose=purpose, selection_eligible=purpose != "test",
            visibility=FeedbackVisibility.HIDDEN if purpose == "test" else FeedbackVisibility.OBSERVED,
            feedback_timing="never" if purpose == "test" else "after_batch" if purpose == "validation" else "after_prediction")

    counts = {split: sum(item.task.split == split for item in dataset.records) for split in ("train", "validation", "test")}
    for seed in settings.seeds:
        for mode in settings.modes:
            offline = mode == "offline"
            epochs = settings.num_epochs if offline else 1
            options = AceOptions(mode=mode, train="learn", validation="validation" if offline else None,
                stream=None if offline else "stream", test_evaluations=("test",) if offline else (),
                reflection_stage="reflect", curation_stage="curate", num_epochs=epochs,
                **{key: getattr(settings, key) for key in ("max_num_rounds", "curator_frequency", "eval_steps",
                    "online_eval_frequency", "token_budget", "no_ground_truth", "json_mode")})
            n, t, v = counts["train"] if offline else counts["test"], counts["test"], counts["validation"]
            maximum = 2 * t + epochs * ((2 + options.max_num_rounds) * n
                + (n // options.eval_steps) * v) if offline else (4 + options.max_num_rounds) * t
            role_calls = epochs * (n * max(options.max_num_rounds, 1) + n // options.curator_frequency)
            evaluations = (evaluation("learn", "train", "optimization"),
                evaluation("validation", "validation", "validation"), evaluation("test", "test", "test")) if offline else (
                evaluation("learn", "test", "optimization"), evaluation("stream", "test", "test"))
            yield RecipeConfig(name=f"ace-formula-seed-{seed}-{mode}", seed=seed,
                method=plugin("proteus.ace", **options.to_dict()),
                reference=ReferenceSpec(profile="adapted", code=REPOSITORY, revision=REVISION, deviations=DEVIATIONS),
                actors={role: getattr(settings, role) for role in ("generator", "reflector", "curator")},
                subject=SubjectSpec(runner=plugin("proteus.ace_subject"), initial_artifact=initial, actor="generator"),
                execution=ExecutionSpec(seed=seed, environment=EnvironmentSpec(backend=plugin("proteus.local_json"),
                    provider="host", network="unrestricted")),
                stages=tuple(StageSpec(id=name, operator=plugin(f"proteus.ace_{name}"), role=role,
                    read_scopes=(f"proteus.ace.{port}_input@1",), write_scopes=(f"proteus.ace.{port}@1",))
                    for name, role, port in (("reflect", "reflector", "reflection"), ("curate", "curator", "curation"))),
                evaluations=evaluations, budget=BudgetLimits(max_evaluations=maximum, max_model_calls=maximum + role_calls))


def prepare(root, dataset, settings, *, sources, packages=(), provenance=None):
    """The Python API accepts explicitly labelled custom/fixture data as well."""
    root = Path(root).resolve()
    with run_lock(root):
        require(not root.exists() or not any(root.iterdir()), "study directory already contains data")
        reg = registry()
        initial = ArtifactStore(root / "artifact-store", reg).put(CODEC, Playbook.from_text())
        publish_bytes(root / "tasks.json", (dataset.to_json() + "\n").encode())
        provenance = provenance if provenance is not None else {"dataset": "custom; not a pinned Formula benchmark"}
        publish_bytes(root / "provenance.json", canonical_json(provenance).encode())
        entries = []
        for recipe in make_recipes(root, dataset, settings, initial):
            recipe = reg.resolve(recipe)
            lock = reg.make_lock(recipe, sources, packages=packages)
            entry = StudyRun(seed=recipe.seed, mode=recipe.method.options["mode"],
                             config_hash=recipe.config_hash, lock_hash=content_hash(lock))
            publish_bytes(root / "recipes" / (entry.name + ".json"), (recipe.to_json() + "\n").encode())
            publish_bytes(root / "locks" / (entry.name + ".json"), (lock.to_json() + "\n").encode())
            entries.append(entry)
        study = Study(dataset_hash=content_hash(dataset), provenance_hash=content_hash(provenance),
                      controller_hash=controller_hash(), settings=settings, runs=tuple(entries))
        publish_bytes(root / "study.json", (study.to_json() + "\n").encode())
    return root / "study.json"


def load(root, *, check_runtime=True):
    root = Path(root).resolve()
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
        require(recipe.seed == entry.seed and recipe.method.options["mode"] == entry.mode, "study seed/mode differs from recipe")
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
            ace_formula_audit.run_report(child, entry, recipe, lock, dataset, reg)
        return report(root, locked=True)


def report(root, *, locked=False):
    root = Path(root).resolve()
    if not locked:
        with run_lock(root):
            return report(root, locked=True)
    study, dataset, reg, runs = load(root, check_runtime=False)
    rows = [{"seed": entry.seed, "mode": entry.mode,
             **ace_formula_audit.run_report(root / "runs" / entry.name, entry, recipe, lock, dataset, reg)}
            for entry, recipe, lock in runs]
    aggregate = {}
    for mode in study.settings.modes:
        aggregate[mode] = {}
        for metric in ("baseline_accuracy", "accuracy", "accuracy_delta"):
            items = [row[metric] for row in rows if row["mode"] == mode]
            aggregate[mode][metric] = {"n": len(items), "mean": statistics.mean(items),
                "sample_sd": statistics.stdev(items) if len(items) > 1 else None}
    return {"study_hash": content_hash(study), "dataset_hash": study.dataset_hash,
        "dataset_profile": "pinned ACE Formula 500/300/200" if study.dataset_hash == data.DATASET_HASH
            else "custom/fixture; not Formula reproduction",
        "settings": study.settings.to_dict(), "deviations": list(DEVIATIONS), "runs": rows, "aggregate": aggregate,
        "notice": "Offline accuracy is held-out test accuracy of the validation-selected playbook. Online accuracy is a "
                  "weighted pre-learning window score, not independent held-out performance. The empty-playbook "
                  "baseline is measured separately inside each run before learning. Unknown cost/tokens stay unknown; "
                  "ledger wall_seconds sums charged operation time, not study elapsed time. "
                  "Completion does not establish provider determinism or equality to published scores."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare")
    prep.add_argument("--out", type=Path, required=True)
    prep.add_argument("--data", type=Path, required=True, help="directory prepared by proteus.benchmarks.ace_formula_data")
    prep.add_argument("--source", type=Path, required=True, help="clean Proteus Git checkout")
    prep.add_argument("--settings", type=Path, required=True, help="Settings JSON; credentials stay in environment")
    for name in ("run", "resume", "report"):
        commands.add_parser(name).add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        dataset = TaskDataset.from_json((args.data / "tasks.json").read_text())
        require(content_hash(dataset) == data.DATASET_HASH, "CLI requires the complete pinned Formula dataset")
        source = source_pin("proteus", args.source)
        require(source.dirty_patch_hash is None, "freeze a clean source commit before preparing a real study")
        print(prepare(args.out, dataset, Settings.from_json(args.settings.read_text()), sources=[source],
            packages=installed_packages(), provenance=read_json((args.data / "provenance.json").read_text())))
    else:
        result = report(args.out) if args.command == "report" else execute(args.out, resume=args.command == "resume")
        print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2))


if __name__ == "__main__":
    main()
