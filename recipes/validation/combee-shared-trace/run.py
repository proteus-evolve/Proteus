"""Run four real method loops against one immutable, model-free learning trace."""

# ruff: noqa: E402 -- executable recipe bootstraps its repository import path
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from proteus.artifacts.store import ArtifactStore
from proteus.core import timing
from proteus.core.contracts import ArtifactRef, BudgetLimits, PluginRef, RuntimeIdentity, require
from proteus.core.methods import ActorResult
from proteus.core.plugins import Plugin, implementation_hash
from proteus.core.serialization import Contract, canonical_json, content_hash
from proteus.evaluation.records import zero_usage
from proteus.evaluation.tasks import Task, TaskDataset, TaskRecord
from proteus.examples.ace_playbook import prepare as prepare_ace, register as register_fixture
from proteus.examples.candidate_evaluation import plugin
from proteus.examples.combee_ace import prepare as prepare_combee
from proteus.methods.ace.batch import CombeeOptions, SampleOptions, register as register_methods
from proteus.methods.ace.method import AceOptions
from proteus.operators.combee.batching import DynamicBatchOptions
from proteus.operators.combee.grouping import Grouping, GroupPlan
from proteus.recipes import load_recipe
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.manifest import SourcePin
from proteus.recipes.runner import run_recipe

HERE = Path(__file__).resolve().parent
VARIANTS = ("ace", "naive_parallel", "combee_fixed", "combee_dynamic")


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    Path(path).write_text(json.dumps(json.loads(canonical_json(value)), indent=2, sort_keys=True) + "\n")


def load_trace(path, expected_hash):
    require(digest(path) == expected_hash, "frozen trace bytes changed")
    trace = json.loads(Path(path).read_text())
    require(trace["schema_version"] == 1 and trace["seed"] == 0, "unsupported trace")
    require(len({row["id"] for row in trace["samples"]}) == len(trace["samples"]), "duplicate sample")
    return trace


@dataclass(frozen=True, kw_only=True)
class TraceOptions(Contract):
    role: str
    manifest: str
    manifest_sha256: str


class TraceActor:
    model_call_bound = 0

    def __init__(self, options):
        self.options = options

    def execute(self, spec, request, workspace):
        trace = load_trace(self.options.manifest, self.options.manifest_sha256)
        rows = {row["id"]: row for row in trace["samples"]}
        role, prompt = self.options.role, request.prompt
        if role in ("generator", "reflector"):
            matches = set(re.findall(r"TASK_ID=(sample-[0-9]+)", prompt))
            require(len(matches) == 1, "request must name exactly one frozen sample")
            key = next(iter(matches))
            require(key in rows, "unknown frozen sample")
            field = "prediction_response" if role == "generator" else "reflection_response"
            text = rows[key][field]
            consumed = [key]
            source_hash = content_hash(rows[key])
        else:
            if "**Independent Proposed Operations:**" in prompt:
                section = prompt.split("**Independent Proposed Operations:**", 1)[1].split("**Your Task:**", 1)[0]
                surface = "reducer_proposals"
            else:
                require("**Recent Reflection:**" in prompt, "curation reflection surface missing")
                section = prompt.split("**Recent Reflection:**", 1)[1].split("**Current Playbook:**", 1)[0]
                surface = "original_reflections"
            consumed = re.findall(r"TRACE\[(sample-[0-9]+)\]", section)
            require(bool(consumed) and set(consumed) <= set(rows), "curation lacks frozen trace provenance")
            text = json.dumps({"reasoning": "deterministic fixture curation", "operations": [
                {"type": "ADD", "section": "OTHERS", "content": f"TRACE[{key}] frozen insight"}
                for key in dict.fromkeys(consumed)]})
            source_hash = content_hash({"surface": surface, "text": section})
        receipt = {"role": role, "consumed": consumed, "source_hash": source_hash,
                   "manifest_sha256": self.options.manifest_sha256,
                   "response_hash": content_hash(text), "prompt_hash": content_hash(prompt)}
        with (workspace / "trace-invocations.jsonl").open("a") as stream:
            stream.write(json.dumps(receipt, sort_keys=True) + "\n")
        return ActorResult(text=text, runtime=RuntimeIdentity(runtime=spec.runtime.plugin),
                           usage=zero_usage(), trace=(receipt,))


def register(registry):
    register_methods(registry)
    register_fixture(registry)
    registry.register(Plugin(ref=PluginRef(name="validation.frozen_trace", version="1"), kind="actor",
        options=TraceOptions, factory=TraceActor, source="proteus",
        implementation_hash=implementation_hash({"runner": Path(__file__), "trace": HERE / "trace.json"}),
        capabilities=("session.fresh", "actor.execute", "actor.parallel", "calls.bounded")))


@contextmanager
def controlled_clock(trace):
    """Replace only TimingService's module reference, never the global time module."""
    original = timing.time
    readings = []

    def monotonic():
        index = len(readings)
        value = (index // 2) * 10.0 + (trace["timing_fixture"]["batch_seconds"] if index % 2 else 0)
        readings.append(value)
        return value

    timing.time = SimpleNamespace(monotonic=monotonic)
    try:
        yield readings
    finally:
        timing.time = original


def prepare_arm(root, variant, manifest):
    trace = load_trace(manifest, digest(manifest))
    path = prepare_ace(root) if variant == "ace" else prepare_combee(root, workers=3, batch_size=3)
    recipe = load_recipe(path)
    dataset = TaskDataset(version="shared-frozen-learning-trace-v1", records=tuple(
        TaskRecord(task=Task(id=row["id"], version="1", split="train",
            input={"question": row["question"], "context": row["context"]}), reference=row["reference"])
        for row in trace["samples"]) + tuple(
        TaskRecord(task=Task(id=f"check-{i}", version="1", split="validation",
            input={"question": row["question"], "context": row["context"]}), reference=row["reference"])
        for i, row in enumerate(trace["samples"][:2])))
    (root / "tasks.json").write_text(dataset.to_json() + "\n")
    if variant == "ace":
        options = replace(AceOptions.from_dict(recipe.method.options),
                          test_evaluations=(), eval_steps=100, max_num_rounds=1)
    else:
        options = replace(CombeeOptions.from_dict(recipe.method.options),
                          test_evaluations=(), eval_steps=100, max_num_rounds=1,
                          grouping=Grouping(profile="ace", duplication_factor=2, num_groups=2))
        if variant == "naive_parallel":
            options = replace(options, aggregation_mode="single_pass",
                              grouping=Grouping(profile="ace", duplication_factor=1, num_groups=1))
        if variant == "combee_dynamic":
            options = replace(options, dynamic_batch=DynamicBatchOptions(
                max_batch_size=4, profile_batch_sizes=(1, 2, 4)))
    evaluations = tuple(replace(spec, tasks=replace(spec.tasks,
        dataset_version=dataset.version, dataset_hash=content_hash(dataset),
        task_ids=(tuple(row["id"] for row in trace["samples"]) if spec.id == "train"
                  else ("check-0", "check-1"))))
        for spec in recipe.evaluations if spec.id in ("train", "validation"))
    recipe = replace(recipe, seed=trace["seed"], method=plugin(recipe.method.plugin.name, **options.to_dict()),
        actors={role: replace(actor, runtime=plugin("validation.frozen_trace", role=role,
            manifest=str(manifest.resolve()), manifest_sha256=digest(manifest)))
            for role, actor in recipe.actors.items()},
        evaluations=evaluations, workflows=tuple(replace(workflow, operator=plugin(
            workflow.operator.plugin.name, **replace(SampleOptions.from_dict(workflow.operator.options),
                                                   max_num_rounds=1).to_dict()))
            for workflow in recipe.workflows), budget=BudgetLimits(max_evaluations=200, max_model_calls=200, max_concurrency=3),
        reference=replace(recipe.reference, deviations=(
            "Shared frozen synthetic sample/prediction/reflection trace; no model-quality claim.",
            "Actual ACE/Combee methods and update services; deterministic fixture curator.",
            "Dynamic TimingService uses declared synthetic durations, not measured performance.")))
    path.write_text(recipe.to_json() + "\n")
    registry = builtin_registry()
    register(registry)
    return registry, recipe


def audit_arm(root, variant, trace, registry, recipe, readings, records_file=None):
    records_path = root / "run" / records_file if records_file else sorted((root / "run").glob("execution-*.json"))[-1]
    records = json.loads(records_path.read_text())
    values = {item["ref"]["id"]: item["value"] for item in records["values"]}
    kind = "proteus.ace.summary@1" if variant == "ace" else "proteus.combee.ace_summary@1"
    summary = next(row["value"] for row in records["values"] if row["ref"]["kind"] == kind)
    samples = [values[ref["id"]] for ref in summary["samples"]]
    expected = {row["id"]: row for row in trace["samples"]}
    require([row["task_id"] for row in samples] == list(expected), "sample order or coverage differs")
    for rollout in records["rollouts"]:
        key = rollout["task_id"]
        require(rollout["output"]["response"] == expected[key]["prediction_response"],
                "prediction differs from frozen trace")
    for batch in records["evaluations"]:
        for row in batch["rows"]:
            require([values[ref["id"]]["value"] for ref in row["feedback"]] == [expected[row["task_id"]]["feedback"]],
                    "feedback differs from frozen trace")
    consumption = []
    for sample in samples:
        key = sample["task_id"]
        responses = [values[ref["id"]]["response"] for ref in sample["reflections"]]
        require(responses == [expected[key]["reflection_response"]], "sample reflection differs from frozen trace")
        consumption.append({"task_id": key, "source_record_hash": content_hash(expected[key]),
                            "reflection_response_hash": content_hash(responses[0]),
                            "method_sample": sample})
    candidates = {item["id"]: item for item in records["candidates"]}
    initial = candidates[summary["initial_id"]]["artifact"]["content_hash"]
    require(initial == trace["H0_content_hash"], "H0 changed")
    store = ArtifactStore(root / "run/artifacts", registry)
    final = store.get(ArtifactRef.from_dict(candidates[summary["final_id"]]["artifact"]))
    require(set(re.findall(r"TRACE\[(sample-[0-9]+)\]", final.text)) == set(expected), "final updates lost a sample")
    batches = [values[ref["id"]] for ref in summary.get("batches", [])]
    plans = [row["value"] for row in records["values"] if row["ref"]["kind"] == "proteus.combee.plan@1"]
    resolved_plans = []
    for plan in plans:
        GroupPlan.from_dict(plan)  # independently validates source/copy indices and seeded shuffle
        originals = [values[ref["id"]] for ref in plan["records"]]
        for original in originals:
            require(original["reflection"] in [row["reflection_response"] for row in expected.values()],
                    "group consumed an unfrozen reflection")
        resolved_plans.append({"plan": plan, "originals": originals, "occurrences": [
            [{**occurrence, "record": originals[occurrence["source_index"]]} for occurrence in group]
            for group in plan["groups"]]})
    invocations = [json.loads(line) for path in sorted((root / "run").rglob("trace-invocations.jsonl"))
                   for line in path.read_text().splitlines()]
    require(len(invocations) == len(records["actor_calls"]), "actor receipt count mismatch")
    actual_receipts = []
    for call in records["actor_calls"]:
        receipt = call["result"]["trace"][0]
        require(receipt["prompt_hash"] == content_hash(call["request"]["prompt"])
                and receipt["response_hash"] == content_hash(call["result"]["text"]),
                "actor payload/receipt mismatch")
        require(receipt["role"] == call["role"], "actor role provenance mismatch")
        if call["role"] in ("generator", "reflector"):
            key, = receipt["consumed"]
            field = "prediction_response" if call["role"] == "generator" else "reflection_response"
            require(receipt["source_hash"] == content_hash(expected[key])
                    and call["result"]["text"] == expected[key][field], "actor source provenance mismatch")
        actual_receipts.append(receipt)
    require(Counter(canonical_json(r) for r in actual_receipts) == Counter(canonical_json(r) for r in invocations),
            "execution and independent invocation receipts differ")
    controller = summary.get("batch_controller")
    if variant == "combee_dynamic":
        require(controller["profile_sizes"] == [1, 2, 4] and controller["selected_size"] == 4,
                "controlled dynamic profile or decision changed")
        require(all(o["wall_seconds"] == trace["timing_fixture"]["batch_seconds"]
                    for o in controller["observations"]), "dynamic timing differs from fixture")
    return {"variant": variant, "seed": recipe.seed, "H0": initial,
            "config_hash": json.loads((root / "run/manifest.json").read_text())["config_hash"], "consumption": consumption,
            "method_options": dict(recipe.method.options), "batch_sizes": [len(b["task_ids"]) for b in batches],
            "batches": batches, "grouping_plans": resolved_plans, "batch_controller": controller,
            "controlled_clock_readings": readings, "role_calls": dict(Counter(r["role"] for r in invocations)),
            "actor_receipts": invocations, "candidate_updates": records["candidates"],
            "final_playbook": final.to_dict(), "execution_file": records_path.name, "execution_sha256": digest(records_path),
            "manifest_sha256": digest(root / "run/manifest.json"), "provider_calls": 0}


def execute(out, manifest=HERE / "trace.json"):
    require(not out.exists() or not any(out.iterdir()), "output must be new")
    out.mkdir(parents=True, exist_ok=True)
    trace = load_trace(manifest, digest(manifest))
    source = SourcePin(name="proteus", repository="https://github.com/proteus-evolve/Proteus",
        revision=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip(),
        dirty_patch_hash=None)
    rows = []
    for variant in VARIANTS:
        root = out / variant
        registry, recipe = prepare_arm(root, variant, manifest)
        lock = registry.make_lock(recipe, [source])
        (root / "lock.json").write_text(lock.to_json() + "\n")
        with controlled_clock(trace) as readings:
            result = run_recipe(recipe, registry=registry, lock=lock,
                                root=root / "run", artifact_source=root / "artifact-store")
        require(result.status.value == "completed", result.diagnostic or "method did not complete")
        row = audit_arm(root, variant, trace, registry, recipe, readings)
        count = sum(row["role_calls"].values())
        resumed = run_recipe(recipe, registry=registry, lock=lock, root=root / "run", resume=True)
        require(resumed == result, "completed resume changed result")
        after = sum(len(p.read_text().splitlines()) for p in (root / "run").rglob("trace-invocations.jsonl"))
        require(after == count, "completed resume repeated fixture calls")
        row["completed_resume_extra_actor_calls"] = after - count
        save(root / "audit.json", row)
        rows.append(row)
    require(all([r["source_record_hash"] for r in row["consumption"]] ==
                [r["source_record_hash"] for r in rows[0]["consumption"]] for row in rows),
            "methods did not consume identical frozen source records")
    report = {"schema_version": 1, "source": source.to_dict(), "trace_sha256": digest(manifest),
              "runner_sha256": digest(__file__), "runtime_tree": subprocess.check_output(
                  ["git", "rev-parse", "HEAD:proteus"], cwd=REPO, text=True).strip(), "scope": trace["scope"], "arms": rows,
              "new_provider_calls": 0, "shared_trace_verified": True}
    save(out / "comparison.json", report)
    return report


def verify(out, manifest=HERE / "trace.json"):
    report = json.loads((out / "comparison.json").read_text())
    trace = load_trace(manifest, report["trace_sha256"])
    require(report["runner_sha256"] == digest(__file__), "reader implementation changed")
    require([row["variant"] for row in report["arms"]] == list(VARIANTS), "comparison arm coverage changed")
    registry = builtin_registry()
    register(registry)
    for arm in report["arms"]:
        root = out / arm["variant"]
        require(json.loads((root / "audit.json").read_text()) == arm, "comparison/audit mismatch")
        require(arm["execution_sha256"] == digest(root / "run" / arm["execution_file"]), "execution bytes changed")
        require(arm["manifest_sha256"] == digest(root / "run/manifest.json"), "run manifest bytes changed")
        actual = audit_arm(root, arm["variant"], trace, registry, load_recipe(root / "recipe.json"),
                           arm["controlled_clock_readings"], arm["execution_file"])
        expected = {key: value for key, value in arm.items() if key != "completed_resume_extra_actor_calls"}
        require(json.loads(canonical_json(actual)) == expected, "saved audit differs from actual records")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--verify", action="store_true", help="read existing receipts without rerunning")
    args = parser.parse_args()
    result = (verify if args.verify else execute)(args.out.resolve())
    print(json.dumps({"shared_trace_verified": result["shared_trace_verified"], "provider_calls": 0,
                      "arms": [{k: arm[k] for k in ("variant", "batch_sizes", "role_calls")}
                               for arm in result["arms"]]}))
