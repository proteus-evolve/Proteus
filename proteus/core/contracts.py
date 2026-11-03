"""Method-neutral records for the Proteus 1.0 development API.

These records describe evidence. Execution, visibility filtering, budget enforcement
and persistence are services built on them, not side effects of constructing a record.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from proteus.core.serialization import Contract, ContractError, content_hash


TEXT = {"minLength": 1, "pattern": r"\S"}
DIGEST = {"pattern": r"^sha256:[0-9a-f]{64}$"}
NONNEGATIVE = {"minimum": 0}
UNIQUE = {"uniqueItems": True}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractError(message)


def unique(values, label: str) -> None:
    values = tuple(values)
    require(len(set(values)) == len(values), f"duplicate {label}")


class Outcome(str, Enum):
    SUCCEEDED = "succeeded"
    TASK_FAILED = "task_failed"
    INVALID_CANDIDATE = "invalid_candidate"
    TIMED_OUT = "timed_out"
    INFRA_ERROR = "infra_error"
    SCORE_MISSING = "score_missing"
    CANCELLED = "cancelled"


class ValidationStatus(str, Enum):
    UNCHECKED = "unchecked"
    VALID = "valid"
    INVALID = "invalid"


class OperationStatus(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StatePolicy(str, Enum):
    INDEPENDENT = "independent"
    SEQUENCE = "sequence"
    WORKER = "worker"


class SessionPolicy(str, Enum):
    FRESH = "fresh"
    HANDOFF = "handoff"
    NATIVE = "native"


class FeedbackVisibility(str, Enum):
    HIDDEN = "hidden"
    OBSERVED = "observed"


@dataclass(frozen=True, kw_only=True)
class PluginRef(Contract):
    name: str = field(metadata=TEXT)
    version: str = field(metadata=TEXT)


@dataclass(frozen=True, kw_only=True)
class DataRef(Contract):
    kind: str = field(metadata=TEXT)
    id: str = field(metadata=TEXT)
    content_hash: str = field(metadata=DIGEST)


@dataclass(frozen=True, kw_only=True)
class ArtifactComponent(Contract):
    path: str = field(metadata=TEXT)
    content_hash: str = field(metadata=DIGEST)
    size_bytes: int = field(metadata=NONNEGATIVE)
    executable: bool = False

    def _validate(self):
        import re
        parts = self.path.split("/")
        require(not any(part in ("", ".", "..") for part in parts)
                and "\\" not in self.path and "\x00" not in self.path
                and ":" not in self.path, "component path must be a portable relative path")
        require(not any(re.search(r'[\x00-\x1f<>"|?*]', part) or part.endswith((".", " "))
                        or re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?", part)
                        for part in parts), "component path is not portable across filesystems")


@dataclass(frozen=True, kw_only=True)
class ArtifactRef(Contract):
    content_hash: str = field(metadata=DIGEST)
    codec: PluginRef
    components: tuple[ArtifactComponent, ...] = ()
    runtime: PluginRef | None = None
    environment_hash: str | None = field(default=None, metadata=DIGEST)

    def _validate(self):
        import unicodedata
        paths = {item.path for item in self.components}
        unique((item.path for item in self.components), "artifact component path")
        for path in paths:
            parts = path.split("/")
            require(not any("/".join(parts[:i]) in paths for i in range(1, len(parts))),
                    "artifact component cannot also be a parent directory")
        # Include directory prefixes: Foo/a and foo/b also alias on common hosts.
        aliases = {}
        for path in paths:
            parts = path.split("/")
            for i in range(1, len(parts) + 1):
                prefix = "/".join(parts[:i])
                key = unicodedata.normalize("NFC", prefix).casefold()
                require(key not in aliases or aliases[key] == prefix,
                        "artifact paths collide on a case-insensitive/normalizing filesystem")
                aliases[key] = prefix


@dataclass(frozen=True, kw_only=True)
class Candidate(Contract):
    id: str = field(metadata=TEXT)
    run_id: str = field(metadata=TEXT)
    artifact: ArtifactRef
    created_by: str = field(metadata=TEXT)
    created_attempt_id: str = field(metadata=TEXT)
    parent_ids: tuple[str, ...] = field(default=(), metadata=UNIQUE)
    validation: ValidationStatus = ValidationStatus.UNCHECKED
    evidence: tuple[DataRef, ...] = ()

    def _validate(self):
        require(all(parent.strip() and parent != self.id for parent in self.parent_ids),
                "candidate parents must be nonempty and exclude the candidate itself")


@dataclass(frozen=True, kw_only=True)
class Usage(Contract):
    """None means unknown, including calls that failed before reporting their usage."""

    model_calls: int | None = field(default=None, metadata=NONNEGATIVE)
    input_tokens: int | None = field(default=None, metadata=NONNEGATIVE)
    output_tokens: int | None = field(default=None, metadata=NONNEGATIVE)
    cost_usd: float | None = field(default=None, metadata=NONNEGATIVE)
    wall_seconds: float | None = field(default=None, metadata=NONNEGATIVE)


@dataclass(frozen=True, kw_only=True)
class BudgetLimits(Contract):
    """None is unlimited; zero is a real zero allowance, never an unlimited sentinel."""

    max_model_calls: int | None = field(default=None, metadata=NONNEGATIVE)
    max_evaluations: int | None = field(default=None, metadata=NONNEGATIVE)
    max_cost_usd: float | None = field(default=None, metadata=NONNEGATIVE)
    max_wall_seconds: float | None = field(default=None, metadata=NONNEGATIVE)
    max_concurrency: int = field(default=1, metadata={"minimum": 1})


@dataclass(frozen=True, kw_only=True)
class RuntimeIdentity(Contract):
    runtime: PluginRef
    model: str | None = field(default=None, metadata=TEXT)
    settings: Mapping[str, Any] = field(default_factory=dict)
    environment_hash: str | None = field(default=None, metadata=DIGEST)


@dataclass(frozen=True, kw_only=True)
class Rollout(Contract):
    id: str = field(metadata=TEXT)
    run_id: str = field(metadata=TEXT)
    operation_id: str = field(metadata=TEXT)
    attempt_id: str = field(metadata=TEXT)
    candidate_id: str = field(metadata=TEXT)
    artifact_hash: str = field(metadata=DIGEST)
    task_id: str = field(metadata=TEXT)
    task_version: str = field(metadata=TEXT)
    split: str = field(metadata=TEXT)
    execution_hash: str = field(metadata=DIGEST)
    runtime: RuntimeIdentity
    status: Outcome
    usage: Usage = field(default_factory=Usage)
    output: Any = None
    trace: tuple[DataRef, ...] = ()
    sequence_id: str | None = field(default=None, metadata=TEXT)
    step: int | None = field(default=None, metadata=NONNEGATIVE)
    state_before: DataRef | None = None
    state_after: DataRef | None = None
    diagnostic: str = ""

    def _validate(self):
        require((self.sequence_id is None) == (self.step is None),
                "sequence_id and step must be specified together")


@dataclass(frozen=True, kw_only=True)
class EvaluationRow(Contract):
    rollout_id: str = field(metadata=TEXT)
    task_id: str = field(metadata=TEXT)
    status: Outcome
    score: float | None = None
    metrics: Mapping[str, float | None] = field(default_factory=dict)
    feedback: tuple[DataRef, ...] = ()
    diagnostic: str = ""

    def _validate(self):
        scored = self.status in (Outcome.SUCCEEDED, Outcome.TASK_FAILED)
        require((self.score is not None) == scored,
                "only succeeded/task_failed evaluations have a score; errors use null")
        require(scored or all(value is None for value in self.metrics.values()),
                "unavailable evaluations cannot contain scored metrics")


@dataclass(frozen=True, kw_only=True)
class EvaluationBatch(Contract):
    id: str = field(metadata=TEXT)
    run_id: str = field(metadata=TEXT)
    operation_id: str = field(metadata=TEXT)
    attempt_id: str = field(metadata=TEXT)
    candidate_id: str = field(metadata=TEXT)
    artifact_hash: str = field(metadata=DIGEST)
    evaluator: PluginRef
    protocol_hash: str = field(metadata=DIGEST)
    expected_rollout_ids: tuple[str, ...] = field(metadata={**UNIQUE, "minItems": 1})
    rows: tuple[EvaluationRow, ...] = field(metadata={"minItems": 1})
    visibility: FeedbackVisibility = FeedbackVisibility.HIDDEN
    usage: Usage = field(default_factory=Usage)

    def _validate(self):
        require(tuple(row.rollout_id for row in self.rows) == self.expected_rollout_ids,
                "evaluation rows must match the complete expected rollout order")

    def mean_score(self) -> float | None:
        """A complete cohort only; missing rows never silently change the denominator."""
        if any(row.score is None for row in self.rows):
            return None
        # Divide first to avoid overflow when summing very large finite values.
        import math
        return math.fsum(row.score / len(self.rows) for row in self.rows)


@dataclass(frozen=True, kw_only=True)
class StageRequest(Contract):
    operation_id: str = field(metadata=TEXT)
    attempt_id: str = field(metadata=TEXT)
    run_id: str = field(metadata=TEXT)
    stage: PluginRef
    role: str = field(metadata=TEXT)
    inputs: Mapping[str, DataRef] = field(default_factory=dict)
    read_scopes: tuple[str, ...] = field(default=(), metadata=UNIQUE)
    write_scopes: tuple[str, ...] = field(default=(), metadata=UNIQUE)
    budget: BudgetLimits = field(default_factory=BudgetLimits)


@dataclass(frozen=True, kw_only=True)
class StageResult(Contract):
    operation_id: str = field(metadata=TEXT)
    attempt_id: str = field(metadata=TEXT)
    run_id: str = field(metadata=TEXT)
    status: OperationStatus
    outputs: Mapping[str, DataRef] = field(default_factory=dict)
    usage: Usage = field(default_factory=Usage)
    diagnostic: str = ""


@dataclass(frozen=True, kw_only=True)
class PluginState(Contract):
    namespace: str = field(metadata=TEXT)
    payload_version: int = field(metadata={"minimum": 1})
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class MethodState(Contract):
    run_id: str = field(metadata=TEXT)
    method: PluginRef
    config_hash: str = field(metadata=DIGEST)
    state: PluginState
    checkpoint_cursor: int = field(default=0, metadata=NONNEGATIVE)
    archive: DataRef | None = None
    sampling_cursor: int = field(default=0, metadata=NONNEGATIVE)
    random_state: Mapping[str, Any] = field(default_factory=dict)
    pending_operations: tuple[StageRequest, ...] = ()

    def _validate(self):
        unique((item.operation_id for item in self.pending_operations), "pending operation")
        require(all(item.run_id == self.run_id for item in self.pending_operations),
                "pending operations belong to a different run")


class StateRegistry:
    """Explicit plugin-owned payload schemas and opt-in, one-version migrations.

    Reading never migrates implicitly. Registering a migration does not change stored
    bytes; callers choose when to persist the returned new PluginState.
    """

    def __init__(self):
        self._schemas = {}
        self._migrations = {}

    def register(self, namespace: str, version: int, schema: type[Contract]) -> None:
        PluginState(namespace=namespace, payload_version=version)
        require(isinstance(schema, type) and issubclass(schema, Contract),
                "state schema must be a Contract class")
        key = (namespace, version)
        require(key not in self._schemas, f"state schema already registered: {key}")
        self._schemas[key] = schema

    def restore(self, state: PluginState) -> Contract:
        key = (state.namespace, state.payload_version)
        require(key in self._schemas, f"unsupported plugin state: {key}")
        return self._schemas[key].from_dict(state.payload)

    def register_migration(self, namespace: str, from_version: int, migrate) -> None:
        PluginState(namespace=namespace, payload_version=from_version)
        key = (namespace, from_version)
        require(key in self._schemas and (namespace, from_version + 1) in self._schemas,
                "register both state schemas before their migration")
        require(callable(migrate), "migration must be callable")
        require(key not in self._migrations, f"migration already registered: {key}")
        self._migrations[key] = migrate

    def migrate(self, state: PluginState, target_version: int) -> PluginState:
        require(type(target_version) is int and target_version >= state.payload_version,
                "target version must be an integer at least the stored version")
        self.restore(state)
        while state.payload_version < target_version:
            key = (state.namespace, state.payload_version)
            require(key in self._migrations, f"no migration for {key}")
            payload = self._migrations[key](self.restore(state))
            state = PluginState(namespace=state.namespace, payload_version=state.payload_version + 1,
                                payload=payload.to_dict() if isinstance(payload, Contract) else payload)
            self.restore(state)
        return state


def validate_records(*, run_id: str, candidates: Sequence[Candidate] = (),
                     rollouts: Sequence[Rollout] = (), evaluations: Sequence[EvaluationBatch] = (),
                     requests: Sequence[StageRequest] = (), results: Sequence[StageResult] = ()) -> None:
    """Validate a complete exported record set, including attempts and candidate lineage.

    All referenced parents and operations must be supplied, including a seed/import
    operation for initial candidates. Artifact/evidence bytes are verified by their
    stores, and declared protocols by execution services (later implementation batches).
    """
    require(type(run_id) is str and bool(run_id.strip()), "run_id must be nonempty text")
    for label, records in (("candidate", candidates), ("rollout", rollouts),
                           ("evaluation", evaluations)):
        unique((item.id for item in records), label)
    unique(((item.operation_id, item.attempt_id) for item in requests), "request attempt")
    unique(((item.operation_id, item.attempt_id) for item in results), "result attempt")
    for record in (*candidates, *rollouts, *evaluations, *requests, *results):
        require(record.run_id == run_id, "record belongs to a different run")
    by_candidate = {item.id: item for item in candidates}
    by_rollout = {item.id: item for item in rollouts}
    by_request = {(item.operation_id, item.attempt_id): item for item in requests}
    operations = {}
    for request in requests:
        semantic = request.to_dict()
        semantic.pop("attempt_id")
        digest = content_hash(semantic)
        require(request.operation_id not in operations or operations[request.operation_id] == digest,
                "retry changed operation inputs, role, scopes or budget")
        operations[request.operation_id] = digest
    for result in results:
        require((result.operation_id, result.attempt_id) in by_request,
                "stage result has no matching request attempt")
    completed = [item.operation_id for item in results if item.status == OperationStatus.COMPLETED]
    unique(completed, "completed operation")
    for candidate in candidates:
        require((candidate.created_by, candidate.created_attempt_id) in by_request,
                "candidate references an unknown operation attempt")
        require(all(parent in by_candidate for parent in candidate.parent_ids),
                "candidate references an unknown parent")
    # Iterative topological traversal handles long experiments without recursion limits.
    children = {key: [] for key in by_candidate}
    remaining = {item.id: len(item.parent_ids) for item in candidates}
    for candidate in candidates:
        for parent in candidate.parent_ids:
            children[parent].append(candidate.id)
    ready = [key for key, degree in remaining.items() if degree == 0]
    visited = 0
    while ready:
        parent = ready.pop()
        visited += 1
        for child in children[parent]:
            remaining[child] -= 1
            if remaining[child] == 0:
                ready.append(child)
    require(visited == len(candidates), "cycle in candidate lineage")
    for record in (*rollouts, *evaluations):
        require((record.operation_id, record.attempt_id) in by_request,
                "record references an unknown operation attempt")
        require(record.candidate_id in by_candidate, "record references an unknown candidate")
        require(record.artifact_hash == by_candidate[record.candidate_id].artifact.content_hash,
                "record artifact differs from the recorded candidate")
    for batch in evaluations:
        for row in batch.rows:
            require(row.rollout_id in by_rollout, "evaluation references an unknown rollout")
            rollout = by_rollout[row.rollout_id]
            require(rollout.candidate_id == batch.candidate_id and rollout.task_id == row.task_id,
                    "evaluation candidate/task differs from its rollout")
            if rollout.status not in (Outcome.SUCCEEDED, Outcome.TASK_FAILED):
                require(row.score is None, "an unavailable rollout cannot receive a valid score")
