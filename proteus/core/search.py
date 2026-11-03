"""Public candidate history and independently configurable pure search policies.

The archive never deletes history. Policies choose an active set or parents from a
specific visible, comparable evaluation view; they cannot turn hidden grades into
selection evidence. Trusted Python plugins are not a security sandbox.
"""

from __future__ import annotations

import math
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Literal, Protocol

from proteus.core.contracts import (
    DIGEST, TEXT, UNIQUE, BudgetLimits, Candidate, DataRef, EvaluationBatch, FeedbackVisibility,
    OperationStatus, StageRequest, StageResult, ValidationStatus, require, unique,
)
from proteus.core.serialization import Contract
from proteus.evaluation.records import zero_usage
from proteus.evaluation.tasks import TaskBatch
from proteus.recipes.config import PluginSpec


@dataclass(frozen=True, kw_only=True)
class ArchiveEvaluation(Contract):
    evaluation_id: str = field(metadata=TEXT)
    batch: EvaluationBatch
    cohort: TaskBatch


@dataclass(frozen=True, kw_only=True)
class ArchiveSnapshot(Contract):
    run_id: str = field(metadata=TEXT)
    candidates: tuple[Candidate, ...]
    evaluations: tuple[ArchiveEvaluation, ...]
    decisions: tuple[DataRef, ...] = ()

    def _validate(self):
        unique((item.id for item in self.candidates), "archive candidate")
        unique((item.batch.id for item in self.evaluations), "archive evaluation")
        candidates = {item.id: item for item in self.candidates}
        require(all(item.run_id == self.run_id for item in self.candidates), "archive mixes runs")
        for item in self.evaluations:
            batch = item.batch
            require(batch.run_id == self.run_id and batch.candidate_id in candidates
                    and batch.artifact_hash == candidates[batch.candidate_id].artifact.content_hash,
                    "archive evaluation belongs to another candidate/run")
            require(batch.visibility == FeedbackVisibility.OBSERVED, "archive cannot expose hidden evaluations")

    def candidate(self, candidate_id):
        found = next((item for item in self.candidates if item.id == candidate_id), None)
        require(found is not None, "unknown archive candidate")
        return found

    def children(self, candidate_id):
        self.candidate(candidate_id)
        return tuple(item for item in self.candidates if candidate_id in item.parent_ids)

    def evidence(self, candidate_id, evaluation_id=None):
        self.candidate(candidate_id)
        return tuple(item for item in self.evaluations if item.batch.candidate_id == candidate_id
                     and (evaluation_id is None or item.evaluation_id == evaluation_id))


@dataclass(frozen=True, kw_only=True)
class Objective(Contract):
    # "score" uses the primary score; other names address EvaluationRow.metrics.
    metric: str = field(metadata=TEXT)
    maximize: bool = True


@dataclass(frozen=True, kw_only=True)
class ScoreVector(Contract):
    candidate_id: str = field(metadata=TEXT)
    batch_id: str = field(metadata=TEXT)
    scores: tuple[float, ...] = field(metadata={"minItems": 1})


@dataclass(frozen=True, kw_only=True)
class SelectionView(Contract):
    archive: DataRef
    evaluation_id: str = field(metadata=TEXT)
    protocol_hash: str | None = field(default=None, metadata=DIGEST)
    dimensions: tuple[str, ...] = ()
    objectives: tuple[Objective, ...] = ()
    candidate_ids: tuple[str, ...] | None = None
    vectors: tuple[ScoreVector, ...] = ()
    excluded: Mapping[str, str] = field(default_factory=dict)

    def _validate(self):
        unique((item.candidate_id for item in self.vectors), "selection candidate")
        if self.candidate_ids is not None:
            unique(self.candidate_ids, "selection population")
            require(all(item.candidate_id in self.candidate_ids for item in self.vectors),
                    "selection candidate outside declared population")
            require(tuple(item.candidate_id for item in self.vectors) == tuple(
                candidate_id for candidate_id in self.candidate_ids
                if candidate_id in {item.candidate_id for item in self.vectors}),
                "selection population order differs")
        require(all(len(item.scores) == len(self.dimensions) for item in self.vectors), "score dimensions differ")
        require(not set(self.excluded).intersection(item.candidate_id for item in self.vectors),
                "selection candidate is both eligible and excluded")


@dataclass(frozen=True, kw_only=True)
class PolicyResult(Contract):
    selected: tuple[str, ...] = field(default=(), metadata=UNIQUE)
    reasons: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class SearchDecision(Contract):
    operation_id: str = field(metadata=TEXT)
    role: Literal["parent", "archive", "final"]
    policy: PluginSpec
    view: DataRef
    result: PolicyResult
    rng_before: Any
    rng_after: Any


class ParentSelector(Protocol):
    def select_parent(self, view: SelectionView, rng) -> PolicyResult: ...


class ArchivePolicy(Protocol):
    def retain(self, view: SelectionView, rng) -> PolicyResult: ...


class FinalSelector(Protocol):
    def select_final(self, view: SelectionView, rng) -> PolicyResult: ...


def _result(view, selected, selected_reason, other_reason):
    return PolicyResult(selected=tuple(selected), reasons={item.candidate_id:
        selected_reason if item.candidate_id in selected else other_reason for item in view.vectors})


class ScalarBest:
    def select_parent(self, view, rng):
        require(bool(view.vectors), "no eligible scored candidates")
        require(len(view.objectives) <= 1, "scalar best requires per-task scores or one aggregate objective")
        # Stable archive order breaks ties; do not use lexicographic candidate IDs.
        best = max(view.vectors, key=lambda item: math.fsum(x / len(item.scores) for x in item.scores))
        return _result(view, (best.candidate_id,), "maximum mean; earliest candidate breaks ties", "lower mean or later tie")

    select_final = select_parent


class UniformParent:
    def select_parent(self, view, rng):
        require(bool(view.vectors), "no eligible scored candidates")
        chosen = rng.choice(view.vectors).candidate_id
        return _result(view, (chosen,), "uniform draw", "not drawn")


class KeepAll:
    def retain(self, view, rng):
        return _result(view, tuple(item.candidate_id for item in view.vectors), "eligible scored candidate", "")


class ParetoFront:
    """Generic non-dominated vectors; this is not GEPA's parent-sampling algorithm."""

    def retain(self, view, rng):
        selected = []
        for item in view.vectors:
            dominated = any(all(a >= b for a, b in zip(other.scores, item.scores))
                and any(a > b for a, b in zip(other.scores, item.scores)) for other in view.vectors)
            if not dominated:
                selected.append(item.candidate_id)
        return _result(view, selected, "non-dominated (including exact ties)", "dominated")

    select_final = retain


class SearchService:
    def __init__(self, services):
        self.services = services

    def snapshot(self):
        def capture():
            services = self.services
            sessions = {item.operation_id: item for item in services._evaluation_service.sessions}
            evaluations = tuple(ArchiveEvaluation(evaluation_id=sessions[batch.operation_id].spec.id,
                batch=batch, cohort=sessions[batch.operation_id].batch) for batch in services.evaluations)
            decisions = tuple(ref for ref, _ in services.values._values.values() if ref.kind == "proteus.search.decision@1")
            snapshot = ArchiveSnapshot(run_id=services.run_id, candidates=tuple(services.candidates),
                                       evaluations=evaluations, decisions=decisions)
            return services.values.publish("proteus.archive@1", snapshot)
        return self.services._record("archive.snapshot", {}, capture, decode=DataRef.from_dict)

    def view(self, archive_ref, evaluation_id, *, protocol_hash=None, objectives=(), candidate_ids=None):
        return self.services._record("archive.view", {"archive": archive_ref, "evaluation_id": evaluation_id,
            "protocol_hash": protocol_hash, "objectives": objectives, "candidate_ids": candidate_ids},
            lambda: self._view(archive_ref, evaluation_id, protocol_hash, objectives, candidate_ids), decode=DataRef.from_dict)

    def _view(self, archive_ref, evaluation_id, protocol_hash, objectives, candidate_ids=None):
        services = self.services
        archive = services.values.get(archive_ref)
        require(type(archive) is ArchiveSnapshot and archive.run_id == services.run_id, "expected this run's archive")
        require(archive.candidates == tuple(services.candidates[:len(archive.candidates)])
                and tuple(item.batch for item in archive.evaluations) == tuple(services.evaluations[:len(archive.evaluations)]),
                "archive differs from committed candidate/evaluation history")
        for evidence in archive.evaluations:
            session = services.evaluation_session(evidence.batch.operation_id)
            require(evidence.evaluation_id == session.spec.id and evidence.cohort == session.batch,
                    "archive evaluation protocol differs from its original session")
        spec = next((item for item in services.recipe.evaluations if item.id == evaluation_id), None)
        require(spec is not None and spec.selection_eligible and spec.purpose != "test"
                and spec.visibility == FeedbackVisibility.OBSERVED, "evaluation is not eligible for selection")
        require(all(type(item) is Objective for item in objectives), "expected typed objectives")
        unique((item.metric for item in objectives), "selection objective")
        if candidate_ids is not None:
            require(type(candidate_ids) is tuple and all(type(item) is str for item in candidate_ids),
                    "candidate_ids must be an ordered tuple of candidate IDs")
            unique(candidate_ids, "selection population")
            require(set(candidate_ids) <= {item.id for item in archive.candidates}, "unknown population candidate")
        evaluations = [item for item in archive.evaluations if item.evaluation_id == evaluation_id]
        protocols = {item.batch.protocol_hash for item in evaluations}
        if protocol_hash is None:
            require(len(protocols) <= 1, "multiple evaluation cohorts/protocols; select an explicit protocol_hash")
            protocol_hash = next(iter(protocols), None)
        else:
            require(protocol_hash in protocols, "unknown evaluation protocol")
        latest = {item.batch.candidate_id: item for item in evaluations if item.batch.protocol_hash == protocol_hash}
        vectors, excluded, dimensions = [], {}, ()
        for candidate in archive.candidates:
            if candidate_ids is not None and candidate.id not in candidate_ids:
                excluded[candidate.id] = "outside declared population"
                continue
            evidence = latest.get(candidate.id)
            if candidate.validation == ValidationStatus.INVALID:
                excluded[candidate.id] = "invalid candidate"
                continue
            if evidence is None:
                excluded[candidate.id] = "not evaluated under selected protocol"
                continue
            rows = evidence.batch.rows
            if tuple(row.task_id for row in rows) != tuple(task.id for task in evidence.cohort.tasks):
                excluded[candidate.id] = "candidate has only part of the cohort"
                continue
            if objectives:
                scores = []
                for objective in objectives:
                    values = [row.score if objective.metric == "score" else row.metrics.get(objective.metric) for row in rows]
                    scores.append(None if any(x is None for x in values) else
                        (1 if objective.maximize else -1) * math.fsum(x / len(values) for x in values))
                names = tuple(("max:" if item.maximize else "min:") + item.metric for item in objectives)
            else:
                scores = [row.score for row in rows]
                names = tuple(f"score:{index}:{row.task_id}" for index, row in enumerate(rows))
            if any(x is None for x in scores):
                excluded[candidate.id] = "missing or failed score/objective; no imputation"
                continue
            require(not dimensions or dimensions == names, "comparable protocol has different score dimensions")
            dimensions = names
            vectors.append(ScoreVector(candidate_id=candidate.id, batch_id=evidence.batch.id, scores=tuple(scores)))
        if candidate_ids is not None:
            by_id = {item.candidate_id: item for item in vectors}
            vectors = [by_id[item] for item in candidate_ids if item in by_id]
        view = SelectionView(archive=archive_ref, evaluation_id=evaluation_id, protocol_hash=protocol_hash,
            dimensions=dimensions, objectives=tuple(objectives), candidate_ids=candidate_ids,
            vectors=tuple(vectors), excluded=excluded)
        return services.values.publish("proteus.search.view@1", view)

    def choose(self, role, policy_name, view_ref, *, operation_id=None):
        operation_id = operation_id or f"op-{len(self.services._operations) + 1:06d}"
        return self.services._record("search." + role, {"policy_name": policy_name, "view": view_ref,
            "operation_id": operation_id}, lambda: self._choose(role, policy_name, view_ref, operation_id),
            decode=SearchDecision.from_dict)

    def _choose(self, role, policy_name, view_ref, operation_id):
        services = self.services
        require(policy_name in services.recipe.search, "unknown declared search policy")
        spec = services.recipe.search[policy_name]
        definition = services.registry.get(spec.plugin, "policy")
        require("search." + role in definition.capabilities, "search policy does not support this role")
        view = services.values.get(view_ref)
        require(type(view) is SelectionView, "expected a selection view")
        require(self._view(view.archive, view.evaluation_id, view.protocol_hash, view.objectives, view.candidate_ids) == view_ref,
                "selection view differs from its eligible archive evidence")
        services._check_operation(operation_id)
        services._operations.add(operation_id)
        services.requests.append(StageRequest(run_id=services.run_id, operation_id=operation_id,
            attempt_id="attempt-1", stage=spec.plugin, role="framework", inputs={"view": view_ref},
            read_scopes=("proteus.search.view@1",), write_scopes=("proteus.search.decision@1",),
            budget=BudgetLimits(max_model_calls=0)))
        before = services.rng.getstate()
        started = time.monotonic()
        try:
            policy = services.registry.create(spec, "policy")
            method = {"parent": "select_parent", "archive": "retain", "final": "select_final"}[role]
            result = getattr(policy, method)(view, services.rng)
            eligible = {item.candidate_id for item in view.vectors}
            require(type(result) is PolicyResult and set(result.selected) <= eligible,
                    "policy selected a candidate outside its eligible view")
            require(set(result.reasons) == eligible and all(result.reasons.values()), "policy must explain every eligible candidate")
            require(role == "archive" or bool(result.selected), "selector returned no candidates")
            result = PolicyResult(selected=result.selected, reasons={**view.excluded, **result.reasons})
            decision = SearchDecision(operation_id=operation_id, role=role, policy=spec, view=view_ref,
                                      result=result, rng_before=before, rng_after=services.rng.getstate())
            ref = services.values.publish("proteus.search.decision@1", decision)
        except Exception as exc:
            services.results.append(StageResult(run_id=services.run_id, operation_id=operation_id,
                attempt_id="attempt-1", status=OperationStatus.FAILED,
                usage=replace(zero_usage(), wall_seconds=time.monotonic() - started), diagnostic=f"{type(exc).__name__}: {exc}"))
            raise
        services.results.append(StageResult(run_id=services.run_id, operation_id=operation_id,
            attempt_id="attempt-1", status=OperationStatus.COMPLETED, outputs={"decision": ref},
            usage=replace(zero_usage(), wall_seconds=time.monotonic() - started)))
        return decision
