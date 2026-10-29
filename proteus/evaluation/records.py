"""Evaluation evidence and the deliberately smaller view returned to methods."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from proteus.core.contracts import (
    DIGEST, TEXT, DataRef, EvaluationBatch, EvaluationRow, Outcome, PluginRef, Rollout, RuntimeIdentity, Usage, require,
)
from proteus.core.serialization import Contract
from proteus.evaluation.tasks import StateValue


@dataclass(frozen=True, kw_only=True)
class Trace(Contract):
    events: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True, kw_only=True)
class Feedback(Contract):
    value: Any = None


@dataclass(frozen=True, kw_only=True)
class BackendEvaluation(Contract):
    """Private verifier evidence from an integrated backend, never an observation.

    The trusted subject adapter supplies this channel; candidate-authored stdout
    must not be interpreted as verifier evidence. The service checks the producer,
    task and candidate binding before publishing a prediction or releasing feedback.
    """

    producer: PluginRef
    artifact_hash: str = field(metadata=DIGEST)
    task_hash: str = field(metadata=DIGEST)
    trial_id: str = field(metadata=TEXT)
    payload: Mapping[str, Any]
    usage: Usage = field(default_factory=Usage)


@dataclass(frozen=True, kw_only=True)
class SubjectResult(Contract):
    loaded_artifact_hash: str = field(metadata=DIGEST)
    runtime: RuntimeIdentity
    status: Outcome
    output: Any = None
    state: StateValue = field(default_factory=StateValue)
    trace: Trace = field(default_factory=Trace)
    usage: Usage = field(default_factory=Usage)
    diagnostic: str = ""
    backend_evaluation: BackendEvaluation | None = None


@dataclass(frozen=True, kw_only=True)
class Grade(Contract):
    status: Outcome
    score: float | None
    metrics: Mapping[str, float | None] = field(default_factory=dict)
    feedback: Feedback = field(default_factory=Feedback)
    usage: Usage = field(default_factory=Usage)
    diagnostic: str = ""
    runtime: RuntimeIdentity | None = None

    def _validate(self):
        EvaluationRow(rollout_id="validation", task_id="validation", status=self.status,
                      score=self.score, metrics=self.metrics)


@dataclass(frozen=True, kw_only=True)
class Observation(Contract):
    rollout: Rollout
    row: EvaluationRow | None = None
    feedback: Feedback | None = None

    def _validate(self):
        require((self.row is None) == (self.feedback is None), "feedback requires a visible evaluation row")
        if self.row is not None:
            require(self.row.rollout_id == self.rollout.id and self.row.task_id == self.rollout.task_id,
                    "observation row differs from its prediction")


@dataclass(frozen=True, kw_only=True)
class EvaluationReceipt(Contract):
    id: str = field(metadata=TEXT)
    task_batch_hash: str = field(metadata=DIGEST)
    rollout_ids: tuple[str, ...]
    candidate_ids: tuple[str, ...]
    # Hidden evaluations return no score, feedback, diagnostic or private record hash.
    batches: tuple[EvaluationBatch, ...] = ()

    def _validate(self):
        require(len(self.rollout_ids) == len(self.candidate_ids), "receipt candidate/rollout length mismatch")


@dataclass(frozen=True, kw_only=True)
class TrialRecord(Contract):
    rollout: Rollout
    cache_key: str = field(metadata=DIGEST)
    task_hash: str = field(metadata=DIGEST)
    history_hash: str = field(metadata=DIGEST)
    task_seed: int
    inputs: Mapping[str, DataRef] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class StateUpdate(Contract):
    after_rollout_id: str = field(metadata=TEXT)
    before: DataRef
    after: DataRef


def sum_usage(values) -> Usage:
    values = tuple(values)
    fields = ("model_calls", "input_tokens", "output_tokens", "cost_usd", "wall_seconds")
    return Usage(**{key: None if any(getattr(value, key) is None for value in values)
                    else sum(getattr(value, key) for value in values) for key in fields})


def zero_usage(*, wall_seconds: float = 0) -> Usage:
    return Usage(model_calls=0, input_tokens=0, output_tokens=0, cost_usd=0, wall_seconds=wall_seconds)
