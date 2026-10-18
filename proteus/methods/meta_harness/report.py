"""Private, read-only audit of Meta-Harness selection and held-out test results."""

import argparse
from collections import Counter
import importlib
from pathlib import Path
import sqlite3

from proteus.core.contracts import DataRef, Outcome, require
from proteus.core.recovery import _load
from proteus.core.run_lock import run_lock
from proteus.core.serialization import canonical_json, content_hash, read_json
from proteus.evaluation.audit import RunAudit
from proteus.evaluation.tasks import TaskDataset, TaskSelection, make_batch
from proteus.execution.code_records import CodeCall, CodeResult, CodeState
from proteus.methods.meta_harness.data import DATASET_HASHES, SIZES
from proteus.methods.meta_harness.records import (
    BenchmarkResult,
    MetaOptions,
    MetaSummary,
    REVISION,
    final_systems,
    frontier,
)
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.manifest import RunManifest


def report(root, registry, *, locked=False):
    root = Path(root).resolve()
    if not locked:
        with run_lock(root):
            return report(root, registry, locked=True)
    manifest = RunManifest.from_json((root / "manifest.json").read_text())
    recipe = manifest.recipe
    audit = RunAudit(root, recipe, manifest.lock, registry)
    require(recipe.method.plugin.name == "proteus.meta_harness", "report requires Meta-Harness")
    options = MetaOptions.from_dict(recipe.method.options)
    require(set(audit.result.outputs) == {"summary"}, "Meta-Harness output contract differs")
    summary = audit.values.get(audit.result.outputs["summary"])
    require(
        type(summary) is MetaSummary and summary.revision == REVISION and summary.finalized,
        "report requires a finalized Meta-Harness experiment",
    )
    benchmarks = [audit.values.get(ref) for ref in summary.benchmarks]
    require(all(type(row) is BenchmarkResult for row in benchmarks), "invalid benchmark record")
    require(
        all(row.complete for row in benchmarks),
        "benchmark has incomplete candidate groups; inspect diagnostics before publishing results",
    )
    require(
        len({(row.system, row.dataset, row.seed) for row in benchmarks}) == len(benchmarks),
        "duplicate benchmark groups",
    )
    expected_frontier = frontier(benchmarks, options.datasets)
    require(
        canonical_json(summary.frontier) == canonical_json(expected_frontier)
        and summary.selected == final_systems(options.baselines, expected_frontier),
        "final selection differs",
    )
    require(len(summary.iterations) == options.iterations, "proposer iteration coverage differs")
    evaluations = {evaluation.id: evaluation for evaluation in recipe.evaluations}
    datasets, pools = {}, {}
    for key, spec in evaluations.items():
        path = root / "private/datasets" / (spec.tasks.dataset_hash.split(":")[1] + ".json")
        # Offline training has no prediction cohort, but shares its dataset with
        # validation/test in the supplied domain recipe. Custom split files are
        # read through their pinned suite if no private snapshot exists.
        dataset = (
            TaskDataset.from_json(path.read_text())
            if path.exists()
            else registry.create(spec.tasks.suite, "task_suite").load()
        )
        datasets[key], pools[key] = dataset, make_batch(dataset, spec.tasks)
    requests = {row["operation_id"]: row for row in audit.records["requests"]}
    code_calls = {}
    for row in audit.records["results"]:
        request = requests[row["operation_id"]]
        if request["stage"]["name"] != "proteus.code_subject":
            continue
        call = audit.values.get(DataRef.from_dict(request["inputs"]["call"]))
        result = audit.values.get(DataRef.from_dict(row["outputs"]["result"]))
        require(
            type(call) is CodeCall
            and type(result) is CodeResult
            and content_hash(call) == result.call_hash,
            "candidate learning request/result mismatch",
        )
        candidate = audit.values.get(DataRef.from_dict(request["inputs"]["candidate"]))
        require(
            candidate == audit.candidates[result.candidate_id]
            and result.state.artifact_hash == candidate.artifact.content_hash,
            "candidate code result binding differs",
        )
        code_calls[result.operation_id] = (call, result)
    with sqlite3.connect((root / "private/ledger.sqlite3").as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        for operation, (_, result) in code_calls.items():
            entry = db.execute(
                "SELECT settlement_json FROM entries WHERE id=?",
                ("code-" + content_hash(operation).split(":")[1],),
            ).fetchone()
            require(entry is not None and entry[0] is not None, "code invocation is not settled")
            require(
                CodeResult.from_dict(read_json(entry[0])["result"]["code_result"]) == result,
                "code result differs from settled invocation",
            )
    receipts, ordered, checked_results = [], [], []

    def cohort(ref, evaluation_id, candidate_id, memory, selection=None):
        receipt = audit.values.get(ref)
        checked = audit.fixed_cohort(
            receipt,
            evaluation_id,
            datasets[evaluation_id],
            candidate_id,
            selection=selection,
            inputs={"memory": memory},
        )
        receipts.append(receipt)
        ordered.extend(checked["rollout_ids"])
        final = audit.values.get(audit.rollouts[receipt.rollout_ids[-1]].state_after)
        return checked, CodeState.from_dict(final.payload["code_memory"])

    for record in benchmarks:
        candidate = audit.candidates[record.candidate_id]
        profile = next(
            d for d in options.datasets if (d.name, d.seed) == (record.dataset, record.seed)
        )
        memory = CodeState(artifact_hash=candidate.artifact.content_hash)
        train = pools[profile.train]
        references = {
            row.task.id: row.reference
            for row in datasets[profile.train].records
            if row.task.split == train.split
        }
        count = (len(train.tasks) + options.batch_size - 1) // options.batch_size
        require(
            len(record.learning) == count * options.num_epochs
            and len(record.training) == (count if options.mode == "online" else 0)
            and len(record.epoch_validations)
            == (options.num_epochs if options.mode == "offline" else 0),
            "learning/validation coverage differs",
        )
        best_accuracy, best_memory, best_epoch = -1, None, 0
        for epoch in range(options.num_epochs):
            for batch_index, offset in enumerate(range(0, len(train.tasks), options.batch_size)):
                tasks = train.tasks[offset : offset + options.batch_size]
                observations = None
                if options.mode == "online":
                    ref = record.training[batch_index]
                    _, memory = cohort(
                        ref,
                        profile.train,
                        candidate.id,
                        memory,
                        TaskSelection(task_ids=tuple(t.id for t in tasks)),
                    )
                    observations = [
                        audit.rollouts[key] for key in audit.values.get(ref).rollout_ids
                    ]
                rows = []
                for index, task in enumerate(tasks):
                    target = references[task.id]
                    row = {
                        "input": task.input,
                        "prediction": target["target"],
                        "ground_truth": target["target"],
                        "was_correct": True,
                    }
                    if observations is not None:
                        rollout = observations[index]
                        grade = registry.create(
                            evaluations[profile.train].evaluator, "evaluator"
                        ).grade(task, rollout, target)
                        metadata = [
                            event["metadata"]
                            for ref in rollout.trace
                            for event in audit.values.get(ref).events
                            if event.get("kind") == "prediction_metadata"
                        ]
                        require(len(metadata) == 1, "prediction metadata missing")
                        row.update(
                            prediction=rollout.output,
                            was_correct=bool(grade.score),
                            metadata=metadata[0]["upstream_metadata"],
                        )
                        if grade.metrics:
                            row["metrics"] = dict(grade.metrics)
                    row.update({k: v for k, v in target["extras"].items() if k not in row})
                    rows.append(row)
                learned = audit.values.get(record.learning[epoch * count + batch_index])
                call, result = code_calls[learned.operation_id]
                require(
                    result == learned
                    and result.status == Outcome.SUCCEEDED
                    and result.candidate_id == candidate.id
                    and call
                    == CodeCall(
                        method="learn_from_batch", state=memory, args=(rows,), seed=profile.seed
                    ),
                    "learning labels, batch order or memory input differs",
                )
                memory = result.state
            if options.mode == "offline":
                checked, memory = cohort(
                    record.epoch_validations[epoch], profile.validation, candidate.id, memory
                )
                if checked["accuracy"] > best_accuracy:
                    best_accuracy, best_memory, best_epoch = checked["accuracy"], memory, epoch
        if best_memory is not None and best_epoch < options.num_epochs - 1:
            memory = best_memory
        require(record.memory == memory, "saved best memory differs")
        checked, _ = cohort(record.validation, profile.validation, candidate.id, memory)
        receipt = audit.values.get(record.validation)
        lengths = []
        for task, key in zip(pools[profile.validation].tasks, receipt.rollout_ids):
            metadata = [
                event["metadata"]
                for ref in audit.rollouts[key].trace
                for event in audit.values.get(ref).events
                if event.get("kind") == "prediction_metadata"
            ]
            require(len(metadata) == 1, "validation prompt metadata missing")
            length = metadata[0]["prompt_info"]["prompt_len"] or 0
            lengths.append(max(0, length - len(task.input)) if length else 0)
        require(
            record.total == checked["tasks"]
            and record.correct == sum(row["score"] for row in checked["scores"])
            and record.context_chars == int(sum(lengths) / len(lengths)),
            "validation summary differs",
        )
        checked_results.append(
            {
                "system": record.system,
                "dataset": record.dataset,
                "seed": record.seed,
                "validation_accuracy": checked["accuracy"],
                "context_chars": record.context_chars,
            }
        )
    expected_tests = {
        f"{name}/{profile.name}/{profile.seed}"
        for name in summary.selected
        for profile in options.datasets
    }
    require(set(summary.tests) == expected_tests, "selected test coverage differs")
    tests = []
    for name in summary.selected:
        for profile in options.datasets:
            record = next(
                row
                for row in benchmarks
                if (row.system, row.dataset, row.seed) == (name, profile.name, profile.seed)
            )
            checked, _ = cohort(
                summary.tests[f"{name}/{profile.name}/{profile.seed}"],
                profile.test,
                record.candidate_id,
                record.memory,
            )
            tests.append({"system": name, "dataset": profile.name, "seed": profile.seed, **checked})
    require(ordered == list(audit.rollouts), "benchmark/test rollout order or coverage differs")
    audit.assert_cohort_coverage(receipts)
    journal = []
    for path in sorted((root / "private/recovery/operations").glob("*.request.json")):
        journal.append((_load(path), path))
    freezes = [
        (i, request, path)
        for i, (request, path) in enumerate(journal)
        if request["kind"] == "meta.freeze"
    ]
    require(len(freezes) == 1, "missing or repeated final selection freeze")
    position, request, path = freezes[0]
    frozen = audit.values.get(DataRef.from_dict(request["identity"]["selection"]))
    require(
        frozen
        == MetaSummary(
            benchmarks=summary.benchmarks,
            iterations=summary.iterations,
            frontier=summary.frontier,
            selected=summary.selected,
            tests={},
            finalized=False,
        ),
        "frozen selection differs",
    )
    freeze_result = _load(path.with_name(path.name.replace(".request.json", ".result.json")))
    require(
        freeze_result["request_hash"] == content_hash(request)
        and freeze_result["error"] is None
        and DataRef.from_dict(freeze_result["output"])
        == DataRef.from_dict(request["identity"]["selection"]),
        "selection freeze was not committed",
    )
    test_ids = {d.test for d in options.datasets}
    require(
        all(r["kind"] not in ("stage", "code.call") for r, _ in journal[position + 1 :]),
        "learning or proposing occurred after freeze",
    )
    require(
        all(
            r["identity"]["evaluation_id"] not in test_ids
            for r, _ in journal[:position]
            if r["kind"] == "evaluation.open"
        ),
        "test data was evaluated before selection freeze",
    )
    require(
        audit.ledger["charged"]["evaluations"] == len(audit.rollouts),
        "evaluation accounting differs",
    )
    calls = audit.records["actor_calls"]
    call_counts = [row["usage"]["model_calls"] for row in calls]
    if all(value is not None for value in call_counts):
        require(
            audit.ledger["charged"]["model_calls"] == sum(call_counts),
            "model call accounting differs",
        )
    else:
        require(
            audit.ledger["unknown"]["model_calls"] > 0,
            "opaque model usage was silently treated as zero",
        )
    return {
        "manifest_hash": manifest.manifest_hash,
        "reference_revision": REVISION,
        "profile": recipe.reference.profile,
        "dataset_profile": "complete pinned MCE splits"
        if all(
            all(
                pools[key].dataset_hash == DATASET_HASHES.get(d.name)
                for key in (d.train, d.validation, d.test)
            )
            and tuple(len(pools[key].tasks) for key in (d.train, d.validation, d.test))
            == SIZES.get(d.name)
            for d in options.datasets
        )
        else "custom/fixture data; not the pinned benchmark",
        "actors": {role: actor.to_dict() for role, actor in recipe.actors.items()},
        "deviations": list(recipe.reference.deviations),
        "selected": list(summary.selected),
        "validation": checked_results,
        "tests": tests,
        "frontier": dict(summary.frontier),
        "role_invocations": dict(Counter(row["role"] for row in calls)),
        "ledger": audit.ledger,
        "notice": "Full declared held-out cohorts verified. Provider/model and runtime are those in the manifest. "
        "Fixture actors are not model experiments; this audit does not assert equality to published results.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--plugin", action="append", default=[])
    args = parser.parse_args()
    from proteus.methods.meta_harness.method import register

    registry = builtin_registry()
    register(registry)
    for name in args.plugin:
        module, attribute = name.split(":")
        getattr(importlib.import_module(module), attribute)(registry)
    print(canonical_json(report(args.run, registry)))


if __name__ == "__main__":
    main()
