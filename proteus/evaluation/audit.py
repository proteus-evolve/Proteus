"""Read-only audit helpers for completed research runs and private test cohorts.

These helpers are used by offline reports, never by optimization methods. They
recompute deterministic grades from pinned task references and public predictions.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from proteus.artifacts.store import ArtifactStore
from proteus.core.contracts import Candidate, EvaluationBatch, OperationStatus, Rollout, StatePolicy, require
from proteus.core.ledger import Ledger, TERMINAL
from proteus.core.methods import MethodResult, Values
from proteus.core.serialization import content_hash, read_json
from proteus.evaluation.records import EvaluationReceipt, TrialRecord
from proteus.evaluation.tasks import make_batch
from proteus.recipes.manifest import RunManifest


def ledger_totals(root, manifest):
    with sqlite3.connect((Path(root) / "private/ledger.sqlite3").as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        identities = db.execute("SELECT value FROM identity").fetchall()
        require(len(identities) == 1 and read_json(identities[0][0]) == {"schema_version": 1,
            "run_id": manifest.run_id, "config_hash": manifest.config_hash, "lock_hash": manifest.lock_hash},
            "study ledger identity differs")
        charged, unknown, slots, count = Ledger._balance(db, "run")
        require(slots == 0 and all(row[0] in TERMINAL for row in db.execute("SELECT state FROM entries")),
                "study contains unsettled operations")
        return {"charged": {key: None if value is None else float(value) for key, value in charged.items()},
                "unknown": unknown, "entries": count}


class RunAudit:
    def __init__(self, root, recipe, lock, registry):
        self.root, self.recipe, self.registry = Path(root), recipe, registry
        self.manifest = RunManifest.from_json((self.root / "manifest.json").read_text())
        self.manifest.assert_resume_compatible(recipe, lock)
        self.result = MethodResult.from_json((self.root / "result.json").read_text())
        require(self.result.status == OperationStatus.COMPLETED, "cannot report an incomplete study run")
        self.records = read_json(sorted(self.root.glob("execution-*.json"))[-1].read_text())
        require(self.records["manifest_hash"] == self.manifest.manifest_hash
                and content_hash(self.records["values"]) == self.records["values_hash"],
                "study execution identity/values differ")
        self.values = Values(registry)
        self.values.restore(self.records["values"])
        self.store = ArtifactStore(self.root / "artifacts", registry)
        self.candidates = {item["id"]: Candidate.from_dict(item) for item in self.records["candidates"]}
        self.rollouts = {item["id"]: Rollout.from_dict(item) for item in self.records["rollouts"]}
        require(len(self.candidates) == len(self.records["candidates"])
                and len(self.rollouts) == len(self.records["rollouts"]), "duplicate run evidence identity")
        for candidate in self.candidates.values():
            require(candidate.run_id == self.manifest.run_id and
                    self.store.candidate(candidate.run_id, candidate.id) == candidate, "candidate record differs from store")
            self.store.get(candidate.artifact)
        self.ledger = ledger_totals(self.root, self.manifest)

    def fixed_cohort(self, receipt, evaluation_id, dataset, candidate_id, *, selection=None, inputs=None):
        recipe = self.recipe
        require(type(receipt) is EvaluationReceipt, "expected evaluation receipt")
        require(recipe.execution.state_policy == StatePolicy.INDEPENDENT,
                "fixed-cohort report currently audits independent trials")
        spec = next(item for item in recipe.evaluations if item.id == evaluation_id)
        require("evaluator.deterministic" in self.registry.get(spec.evaluator.plugin).capabilities,
                "offline regrading requires a deterministic evaluator")
        expected = make_batch(dataset, spec.tasks, selection)
        require(receipt.task_batch_hash == content_hash(expected), "receipt task batch differs")
        candidate = self.candidates[candidate_id]
        input_values = inputs or {}
        require(set(input_values) == set(recipe.subject.input_types), "report subject input ports differ")
        scratch = Values(self.registry)
        refs = {key: scratch.publish(recipe.subject.input_types[key], value) for key, value in input_values.items()}
        for key, ref in refs.items():
            require(self.values.get(ref) == input_values[key], "report subject input value differs")
        root = self.root / "evaluations" / content_hash(receipt.id).split(":")[1]
        protocol = read_json((root / "protocol.json").read_text())
        require(protocol == {"evaluation": spec.to_dict(), "batch": expected.to_dict(),
            "execution": recipe.execution.to_dict(), "inputs": {key: ref.to_dict() for key, ref in refs.items()},
            "reserved_evaluations": len(expected.tasks)}, "evaluation protocol differs")
        require(EvaluationReceipt.from_json((root / "receipt.json").read_text()) == receipt,
                "receipt differs from the method result")
        batches = [EvaluationBatch.from_json(path.read_text()) for path in sorted((root / "private/batches").glob("*.json"))]
        require(len(batches) == 1, "fixed cohort requires exactly one complete candidate batch")
        batch = batches[0]
        require(batch.run_id == self.manifest.run_id and batch.candidate_id == candidate_id
                and batch.artifact_hash == candidate.artifact.content_hash and batch.evaluator == spec.evaluator.plugin
                and batch.visibility == spec.visibility and batch.operation_id == receipt.id,
                "cohort candidate/evaluator identity differs")
        require(batch.protocol_hash == content_hash({"spec": spec, "batch": expected, "subject": recipe.subject,
            "execution": recipe.execution, "lock_hash": self.manifest.lock_hash, "inputs": refs}), "protocol hash differs")
        require(tuple(row.task_id for row in batch.rows) == tuple(task.id for task in expected.tasks)
                and tuple(row.rollout_id for row in batch.rows) == batch.expected_rollout_ids == receipt.rollout_ids
                and len(set(receipt.rollout_ids)) == len(receipt.rollout_ids)
                and receipt.candidate_ids == (candidate_id,) * len(expected.tasks), "cohort coverage/order differs")
        require((not receipt.batches) if spec.visibility.value == "hidden" else receipt.batches == (batch,),
                "receipt feedback visibility differs")
        grader = self.registry.create(spec.evaluator, "evaluator")
        references = {(record.task.split, record.task.id): record.reference for record in dataset.records}
        for index, (row, task) in enumerate(zip(batch.rows, expected.tasks)):
            require(row.score is not None, "study stopped: cohort includes unavailable scores")
            rollout = self.rollouts[row.rollout_id]
            execution_hash = content_hash({"execution": recipe.execution, "inputs": refs}) if refs else content_hash(recipe.execution)
            require(rollout.run_id == self.manifest.run_id and rollout.candidate_id == candidate_id
                    and rollout.artifact_hash == candidate.artifact.content_hash and rollout.task_id == task.id
                    and rollout.task_version == task.version and rollout.split == task.split
                    and rollout.operation_id == receipt.id and rollout.execution_hash == execution_hash,
                    "cohort rollout binding differs")
            trial = TrialRecord.from_json((root / "predictions" / f"{index:06d}.json").read_text())
            require(trial.rollout == rollout and trial.task_hash == content_hash(task) and trial.inputs == refs,
                    "prediction evidence differs")
            grade = grader.grade(task, rollout, references[(task.split, task.id)])
            require((row.score, row.status, dict(row.metrics)) == (grade.score, grade.status, dict(grade.metrics)),
                    "cohort grade disagrees with the pinned metric")
        score = batch.mean_score()
        require(score is not None, "cohort includes unavailable scores")
        return {"accuracy": score, "tasks": len(batch.rows), "candidate_id": candidate_id,
            "artifact_hash": candidate.artifact.content_hash,
            "scores": [{"task_id": row.task_id, "score": row.score} for row in batch.rows],
            "rollout_ids": list(receipt.rollout_ids)}

    def assert_cohort_coverage(self, receipts, *, evaluation_ids=None):
        expected = {content_hash(receipt.id).split(":")[1] for receipt in receipts}
        actual = {path.parent.name for path in (self.root / "evaluations").glob("*/protocol.json")
                  if evaluation_ids is None or read_json(path.read_text())["evaluation"]["id"] in evaluation_ids}
        require(actual == expected, "study contains missing or unreported evaluation cohorts")

    def managed_totals(self):
        calls = self.records["actor_calls"]
        require(self.ledger["charged"]["evaluations"] == len(self.rollouts), "study evaluation accounting differs")
        require(all(call["usage"]["model_calls"] is not None for call in calls),
                "study requires known managed model-call counts")
        require(self.ledger["charged"]["model_calls"] == sum(call["usage"]["model_calls"] for call in calls),
                "study actor accounting differs")
        tokens = {key: [call["usage"].get(key) for call in calls] for key in ("input_tokens", "output_tokens")}
        return {"ledger": self.ledger, "tokens": {key: None if None in items else sum(items) for key, items in tokens.items()}}
