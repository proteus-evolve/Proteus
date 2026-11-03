"""Same-protocol Formula studies for ACE, naive parallel and Combee variants."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field, replace
import json
from pathlib import Path
import statistics
from typing import Literal

from proteus.artifacts.store import ArtifactStore, publish_bytes
from proteus.benchmarks import ace_formula as domain, ace_formula_data as data
from proteus.core.contracts import BudgetLimits, OperationStatus, require
from proteus.core.plugins import implementation_hash
from proteus.core.run_lock import run_lock
from proteus.core.serialization import canonical_json, content_hash, read_json
from proteus.evaluation import study_timing
from proteus.evaluation.tasks import TaskDataset, make_batch
from proteus.examples import ace_formula
from proteus.examples.candidate_evaluation import plugin
from proteus.methods.ace.batch import CombeeOptions, SampleOptions, register as register_combee
from proteus.methods.ace.playbook import CODEC, Playbook
from proteus.operators.combee.batching import DynamicBatchOptions
from proteus.operators.combee.grouping import Grouping
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.cli import installed_packages, source_pin
from proteus.recipes.config import StageSpec, WorkflowSpec, load_recipe
from proteus.recipes.manifest import DependencyLock, RunManifest
from proteus.recipes.runner import run_recipe


Variant = Literal["ace", "naive_parallel", "combee_fixed", "combee_dynamic"]
DEVIATIONS = (
    "Common-condition Combee/ACE Formula study; not a reproduction of published scores.",
    "Uses the pinned ACE Formula 500/300/200 files; the Combee paper reports 250 training samples. "
    "The exact paper subset was not established, so this study does not invent that selection.",
    "Each seed/mode uses identical actors, data, initial playbook and hard budget ceilings across variants; "
    "actual calls, cost and elapsed time can differ and are reported separately.",
    "Managed fresh chat roles and bounded parallel trials; transport failures stop without synthetic scores or unbounded retries.",
    "Naive parallel uses the same batch sample learner with one curation pass, no duplication and no reducer.",
    "ACE retains its reference serial loop; batched variants retain ACEBatch's update and boundary-validation rules. "
    "Validation counts may differ by batch size and are reported, not silently equalized.",
    "Dynamic profiling uses ordinary training and measured local wall time; interrupted durations stay unknown.",
    "End-to-end elapsed time includes baseline, optimization, validation, final evaluation and persistence; "
    "it excludes preparation and report auditing and is not summed worker time or paper training latency.",
)


@dataclass(frozen=True, kw_only=True)
class Settings(ace_formula.Settings):
    # One validation at the end of each full 500-task training epoch by default,
    # giving every batch schedule the same selection opportunities.
    eval_steps: int = field(default=500, metadata={"minimum": 1})
    variants: tuple[Variant, ...] = field(default=("ace", "naive_parallel", "combee_fixed", "combee_dynamic"),
                                        metadata={"minItems": 1, "uniqueItems": True})
    batch_size: int = field(default=50, metadata={"minimum": 1})
    max_workers: int = field(default=8, metadata={"minimum": 1})
    dynamic_batch: DynamicBatchOptions = field(default_factory=lambda: DynamicBatchOptions(max_batch_size=100))
    budget: BudgetLimits | None = None

    def _validate(self):
        super()._validate()
        require(self.curator_frequency == 1, "Combee comparison requires a sequential curator at every sample")
        if self.budget:
            require(self.budget.max_evaluations is not None and self.budget.max_model_calls is not None,
                    "explicit study budgets need evaluation and model-call ceilings")


@dataclass(frozen=True, kw_only=True)
class StudyRun(ace_formula.StudyRun):
    variant: Variant

    @property
    def name(self):
        return f"seed-{self.seed}-{self.mode}-{self.variant}"


@dataclass(frozen=True, kw_only=True)
class Study(ace_formula.Study):
    settings: Settings
    runs: tuple[StudyRun, ...]

    def _validate(self):
        require(tuple((row.seed, row.mode, row.variant) for row in self.runs) == tuple(
            (seed, mode, variant) for seed in self.settings.seeds for mode in self.settings.modes
            for variant in self.settings.variants), "study must contain every declared seed/mode/variant")


def registry():
    reg = builtin_registry()
    register_combee(reg)
    domain.register(reg)
    return reg


def controller_hash():
    from proteus.examples import combee_formula_audit
    return implementation_hash({"study": Path(__file__), "base": Path(ace_formula.__file__),
        "audit": Path(combee_formula_audit.__file__), "elapsed": Path(study_timing.__file__)})


def make_recipes(root, dataset, settings, initial):
    for base in ace_formula.make_recipes(root, dataset, settings, initial):
        learning = next(spec for spec in base.evaluations if spec.id == "learn")
        samples = len(learning.tasks.task_ids) * base.method.options["num_epochs"]
        budget = settings.budget or replace(base.budget, max_model_calls=base.budget.max_model_calls + samples)
        budget = replace(budget, max_concurrency=budget.max_concurrency or settings.max_workers)
        for variant in settings.variants:
            recipe = replace(base, name=f"combee-formula-{base.seed}-{base.method.options['mode']}-{variant}",
                budget=budget, reference=replace(base.reference, paper="https://arxiv.org/abs/2604.04247",
                    deviations=DEVIATIONS))
            if variant != "ace":
                common = {key: value for key, value in base.method.options.items()
                          if key not in ("curation_stage", "curator_frequency")}
                options = CombeeOptions.from_dict({**common, "sample_workflow": "learn-sample",
                    "aggregation_stage": "aggregate", "batch_size": settings.batch_size,
                    "max_workers": settings.max_workers,
                    "aggregation_mode": "single_pass" if variant == "naive_parallel" else "combee",
                    "grouping": Grouping(profile="ace", duplication_factor=1 if variant == "naive_parallel" else 2,
                        num_groups=1 if variant == "naive_parallel" else None).to_dict(),
                    "dynamic_batch": settings.dynamic_batch.to_dict() if variant == "combee_dynamic" else None})
                sample = SampleOptions(train=options.train, reflection_stage=options.reflection_stage,
                    max_num_rounds=options.max_num_rounds, no_ground_truth=options.no_ground_truth,
                    json_mode=options.json_mode)
                recipe = replace(recipe, method=plugin("proteus.combee_ace", **options.to_dict()),
                    execution=replace(recipe.execution, max_workers=settings.max_workers),
                    stages=(base.stages[0], StageSpec(id="aggregate", operator=plugin("proteus.prompt"),
                        role="curator", read_scopes=("proteus.text@1",), write_scopes=("proteus.actor_result@1",))),
                    workflows=(WorkflowSpec(id="learn-sample", operator=plugin("proteus.combee_ace_sample", **sample.to_dict()),
                        stages=("reflect",), evaluations=("learn",)),))
            yield variant, recipe


def prepare(root, dataset, settings, *, sources, packages=(), provenance=None):
    root = Path(root).resolve()
    with run_lock(root):
        require(not root.exists() or not any(root.iterdir()), "study directory already contains data")
        reg = registry()
        initial = ArtifactStore(root / "artifact-store", reg).put(CODEC, Playbook.from_text())
        publish_bytes(root / "tasks.json", (dataset.to_json() + "\n").encode())
        provenance = provenance if provenance is not None else {"dataset": "custom; not a pinned Formula benchmark"}
        publish_bytes(root / "provenance.json", canonical_json(provenance).encode())
        entries = []
        for variant, recipe in make_recipes(root, dataset, settings, initial):
            recipe = reg.resolve(recipe)
            lock = reg.make_lock(recipe, sources, packages=packages)
            entry = StudyRun(seed=recipe.seed, mode=recipe.method.options["mode"], variant=variant,
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
        require(recipe.seed == entry.seed and recipe.method.options["mode"] == entry.mode,
                "study seed/mode differs from recipe")
        if check_runtime:
            reg.check_lock(reg.resolve(recipe), lock)
        for spec in recipe.evaluations:
            require(Path(spec.tasks.suite.options["path"]).resolve() == root / "tasks.json", "study dataset path changed")
            make_batch(dataset, spec.tasks)
        runs.append((entry, recipe, lock))
    expected = {(recipe.seed, recipe.method.options["mode"], variant): reg.resolve(recipe)
        for variant, recipe in make_recipes(root, dataset, study.settings, runs[0][1].subject.initial_artifact)}
    require(all(recipe.config_hash == expected[(entry.seed, entry.mode, entry.variant)].config_hash
                for entry, recipe, _ in runs), "study recipe differs from the declared comparison settings")
    return study, dataset, reg, runs


def execute(root, *, resume=False):
    from proteus.examples.combee_formula_audit import run_report
    root = Path(root).resolve()
    with run_lock(root):
        _, dataset, reg, runs = load(root)
        if not resume:
            require(not (root / "runs").exists(), "study has run data; use resume")
        for entry, recipe, lock in runs:
            child = root / "runs" / entry.name
            if child.exists():
                RunManifest.from_json((child / "manifest.json").read_text()).assert_resume_compatible(recipe, lock)
        for entry, recipe, lock in runs:
            child = root / "runs" / entry.name

            def action():
                result = run_recipe(recipe, registry=reg, lock=lock, root=child,
                    artifact_source=root / "artifact-store", resume=(child / "manifest.json").exists())
                require(result.status == OperationStatus.COMPLETED, f"{entry.name} stopped: {result.diagnostic}")

            study_timing.measure(root / "timings" / entry.name, entry.config_hash, entry.lock_hash, action)
            run_report(child, entry, recipe, lock, dataset, reg)
        return report(root, locked=True)


def report(root, *, locked=False):
    from proteus.examples.combee_formula_audit import run_report
    root = Path(root).resolve()
    if not locked:
        with run_lock(root):
            return report(root, locked=True)
    study, dataset, reg, runs = load(root, check_runtime=False)
    rows = [{"seed": entry.seed, "mode": entry.mode, "variant": entry.variant,
        "elapsed": study_timing.read(root / "timings" / entry.name, entry.config_hash, entry.lock_hash).to_dict(),
        **run_report(root / "runs" / entry.name, entry, recipe, lock, dataset, reg)} for entry, recipe, lock in runs]
    aggregates = {}
    for mode in study.settings.modes:
        for variant in study.settings.variants:
            cohort = [row for row in rows if (row["mode"], row["variant"]) == (mode, variant)]
            aggregate = {}
            for metric in ("baseline_accuracy", "accuracy", "accuracy_delta", "elapsed_seconds", "cost_usd", "model_calls"):
                items = [row["elapsed"]["wall_seconds"] if metric == "elapsed_seconds" else
                    row["ledger"]["charged"][metric] if metric in ("cost_usd", "model_calls") else row[metric] for row in cohort]
                known = [item for item in items if item is not None]
                aggregate[metric] = {"n": len(items), "known": len(known),
                    "mean": statistics.mean(known) if len(known) == len(items) else None,
                    "sample_sd": statistics.stdev(known) if len(known) == len(items) and len(known) > 1 else None}
            aggregates[f"{mode}/{variant}"] = aggregate
    return {"study_hash": content_hash(study), "dataset_hash": study.dataset_hash,
        "dataset_profile": "pinned ACE Formula 500/300/200; common-condition Combee study" if study.dataset_hash == data.DATASET_HASH
            else "custom/fixture; not Formula reproduction",
        "settings": study.settings.to_dict(), "deviations": list(DEVIATIONS), "runs": rows, "aggregate": aggregates,
        "notice": "Identical ceilings do not imply identical actual cost. Online scores are pre-learning window scores, "
                  "not independent held-out accuracy. Unknown cost or interrupted elapsed time remains unknown. "
                  "End-to-end elapsed time includes all run work and cannot establish published training speedups."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare")
    for name in ("out", "data", "source", "settings"):
        prep.add_argument("--" + name, type=Path, required=True)
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
