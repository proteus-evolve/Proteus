"""Synchronous method, stage and actor boundaries for the 1.0 plugin API.

Methods own control flow. Side-effecting actor work crosses a service boundary;
stage inputs/outputs are registered immutable records. Durable orchestration and
parallel dispatch are subsequent layers, not implicit behavior of this executor.
"""

from __future__ import annotations

import time
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol

from proteus.core.contracts import (
    TEXT, ArtifactRef, BudgetLimits, Candidate, DataRef, MethodState, OperationStatus, PluginRef,
    RuntimeIdentity, StageRequest, StageResult, Usage, ValidationStatus, require,
)
from proteus.core.serialization import Contract, content_hash
from proteus.recipes.config import ActorSpec, RecipeConfig


@dataclass(frozen=True, kw_only=True)
class ActorRequest(Contract):
    prompt: str = field(metadata=TEXT)
    system: str = ""
    session_id: str | None = field(default=None, metadata=TEXT)
    handoff: str | None = None


@dataclass(frozen=True, kw_only=True)
class ActorResult(Contract):
    text: str
    runtime: RuntimeIdentity
    usage: Usage
    status: OperationStatus = OperationStatus.COMPLETED
    session_id: str | None = field(default=None, metadata=TEXT)
    trace: tuple[Mapping[str, Any], ...] = ()
    diagnostic: str = ""


@dataclass(frozen=True, kw_only=True)
class MethodResult(Contract):
    status: OperationStatus = OperationStatus.COMPLETED
    outputs: Mapping[str, DataRef] = field(default_factory=dict)
    state: MethodState | None = None
    diagnostic: str = ""


@dataclass(frozen=True, kw_only=True)
class ArtifactProposal(Contract):
    """Typed audit envelope; the selected codec validates its private delta schema."""

    codec: PluginRef
    delta: Mapping[str, Any]


class Method(Protocol):
    def run(self, services: RunServices, restored_state: MethodState | None) -> MethodResult: ...


class Stage(Protocol):
    def run(self, context: StageContext, inputs: Mapping[str, Contract]) -> Mapping[str, Contract]: ...


class ActorRuntime(Protocol):
    # None means no defensible call bound. Finite call budgets reject that runtime.
    model_call_bound: int | None

    def execute(self, spec: ActorSpec, request: ActorRequest, workspace: Path) -> ActorResult: ...


class StageFailed(RuntimeError):
    def __init__(self, result: StageResult):
        self.result = result
        super().__init__(result.diagnostic)


def supported_budget(budget: BudgetLimits, *, allow_evaluations: bool = False) -> None:
    require(budget.max_cost_usd is None and budget.max_wall_seconds is None
            and (allow_evaluations or budget.max_evaluations is None) and budget.max_concurrency == 1,
            "execution supports model-call caps, run evaluation caps and serial dispatch; cost/time/"
            "concurrent reservations require the subsequent ledger services")


class Values:
    """Typed values for one invocation. Artifact bytes have their own durable store."""

    def __init__(self, registry):
        self.registry = registry
        self._values = {}

    def publish(self, kind: str, value: Contract) -> DataRef:
        schema = self.registry.data_schema(kind)
        require(type(value) is schema, f"{kind} requires {schema.__name__}")
        digest = content_hash(value)
        ref = DataRef(kind=kind, id=content_hash([kind, digest]), content_hash=digest)
        self._values[ref.id] = (ref, value)
        return ref

    def get(self, ref: DataRef) -> Contract:
        require(ref.id in self._values, f"unknown data reference: {ref.id}")
        stored, value = self._values[ref.id]
        require(stored == ref and content_hash(value) == ref.content_hash, "data reference mismatch")
        return value

    def export(self) -> list[dict]:
        return [{"ref": ref.to_dict(), "value": value.to_dict()}
                for ref, value in self._values.values()]


class StageContext:
    """A stage receives its bound actor and explicitly supplied typed inputs only.

    Scopes constrain data ports and tool declarations, not arbitrary Python/file access.
    Plugins are trusted code; process/container isolation belongs to execution backends.
    """

    def __init__(self, services, spec, request):
        self._services, self.spec, self.request = services, spec, request

    def actor(self, prompt: str, *, session_id: str | None = None,
              handoff: str | None = None) -> ActorResult:
        return self._services._actor(self.spec, self.request, prompt, session_id, handoff)


class RunServices:
    def __init__(self, *, run_id: str, recipe: RecipeConfig, registry, root: Path,
                 prompts: Mapping[str, str] | None = None, resume: bool = False, lock_hash: str | None = None):
        self.run_id, self.recipe, self.registry = run_id, recipe, registry
        self.root, self.resume = Path(root), resume
        self.lock_hash = lock_hash
        self.prompts = dict(prompts or {})
        self.values = Values(registry)
        from proteus.artifacts.store import ArtifactStore
        codecs = (recipe.subject.initial_artifact.codec,) if recipe.subject is not None else ()
        self.artifacts = ArtifactStore(self.root / "artifacts", registry, allowed_codecs=codecs)
        self.candidates = []
        self.rollouts, self.evaluations = [], []
        from proteus.evaluation.service import EvaluationService
        self._evaluation_service = EvaluationService(self)
        self.requests, self.results, self.actor_calls = [], [], []
        self._operations, self._runtime = set(), {}
        self._dispatch_thread = threading.get_ident()
        self._charged = {"run": 0}
        for actor in recipe.actors.values():
            if actor.prompt_hash is not None:
                require(actor.prompt_hash in self.prompts, "role prompt content is missing")
                require(content_hash(self.prompts[actor.prompt_hash]) == actor.prompt_hash,
                        "role prompt content differs from its hash")

    def _actor(self, stage, operation, prompt, session_id, handoff, *, isolate=False):
        require(threading.get_ident() == self._dispatch_thread,
                "P02 services are serial; cross-thread actor dispatch requires the parallel scheduler")
        spec = self.recipe.actors[stage.role]
        if spec.session.value == "fresh":
            require(session_id is None and handoff is None, "fresh actors cannot inherit session state")
        elif spec.session.value == "handoff":
            require(session_id is None, "handoff is not a native session")
        else:
            require(handoff is None, "native sessions cannot silently become handoffs")
        request = ActorRequest(prompt=prompt, system=self.prompts.get(spec.prompt_hash, ""),
                               session_id=session_id, handoff=handoff)
        if isolate:
            runtime = self.registry.create(spec.runtime, "actor")
        elif stage.role not in self._runtime:
            self._runtime[stage.role] = self.registry.create(spec.runtime, "actor")
        if not isolate:
            runtime = self._runtime[stage.role]
        bound = runtime.model_call_bound
        require(bound is None or (type(bound) is int and bound >= 0), "invalid actor call bound")
        limits = (("run", self.recipe.budget), (f"role:{stage.role}", spec.budget),
                  (f"stage:{operation.operation_id}", stage.budget))
        for key, budget in limits:
            supported_budget(budget, allow_evaluations=key == "run")
            if budget.max_model_calls is not None:
                require(bound is not None, "runtime cannot enforce a finite model-call reservation")
                require(self._charged.get(key, 0) + bound <= budget.max_model_calls,
                        f"model-call budget exhausted: {key}")
        for key, _ in limits:
            self._charged[key] = self._charged.get(key, 0) + (bound or 0)
        call_id = f"actor-{len(self.actor_calls) + 1:06d}"
        workspace = self.root / "actors" / call_id
        workspace.mkdir(parents=True, exist_ok=False)
        record = {"id": call_id, "operation_id": operation.operation_id,
                  "attempt_id": operation.attempt_id, "role": stage.role,
                  "request": request.to_dict(), "prompt_hash": content_hash(request),
                  "reserved_model_calls": bound, "result": None}
        self.actor_calls.append(record)
        started = time.monotonic()
        try:
            result = runtime.execute(spec, request, workspace)
            require(type(result) is ActorResult, "actor must return ActorResult")
            require(result.runtime.runtime == spec.runtime.plugin, "actor returned a different runtime")
            actual = result.usage.model_calls
            require(bound is None or actual is None or actual <= bound,
                    "actor exceeded its declared model-call bound")
            record["result"] = result.to_dict()
            # Unknown usage keeps the entire reservation charged.
            if bound is not None and actual is not None:
                for key, _ in limits:
                    self._charged[key] -= bound - actual
            return result
        except Exception as exc:
            record["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            record["wall_seconds"] = time.monotonic() - started

    def stage(self, stage_id: str, inputs: Mapping[str, DataRef], *, operation_id: str | None = None) -> StageResult:
        require(threading.get_ident() == self._dispatch_thread,
                "P02 services are serial; cross-thread stage dispatch requires the parallel scheduler")
        spec = next((item for item in self.recipe.stages if item.id == stage_id), None)
        require(spec is not None, f"unknown stage: {stage_id}")
        require(set(inputs) == set(spec.input_types), f"{stage_id}: input port mismatch")
        values = {}
        for name, ref in inputs.items():
            require(ref.kind == spec.input_types[name], f"{stage_id}.{name}: input data kind mismatch")
            values[name] = self.values.get(ref)
        operation_id = operation_id or f"op-{len(self._operations) + 1:06d}"
        require(operation_id not in self._operations, "operation already submitted")
        self._operations.add(operation_id)
        supported_budget(spec.budget)
        plugin = self.registry.create(spec.operator, "stage")
        for attempt in range(1, spec.max_attempts + 1):
            request = StageRequest(operation_id=operation_id, attempt_id=f"attempt-{attempt}",
                run_id=self.run_id, stage=spec.operator.plugin, role=spec.role, inputs=inputs,
                read_scopes=spec.read_scopes, write_scopes=spec.write_scopes, budget=spec.budget)
            self.requests.append(request)
            first_call = len(self.actor_calls)
            try:
                outputs = plugin.run(StageContext(self, spec, request), MappingProxyType(values))
                require(isinstance(outputs, Mapping) and set(outputs) == set(spec.output_types),
                        f"{stage_id}: output port mismatch")
                # Validate every port before publishing any output.
                for name, value in outputs.items():
                    require(type(value) is self.registry.data_schema(spec.output_types[name]),
                            f"{stage_id}.{name}: output type mismatch")
                refs = {name: self.values.publish(spec.output_types[name], value)
                        for name, value in outputs.items()}
                result = StageResult(operation_id=operation_id, attempt_id=request.attempt_id,
                    run_id=self.run_id, status=OperationStatus.COMPLETED, outputs=refs,
                    usage=self._usage(first_call))
            except Exception as exc:
                result = StageResult(operation_id=operation_id, attempt_id=request.attempt_id,
                    run_id=self.run_id, status=OperationStatus.FAILED, usage=self._usage(first_call),
                    diagnostic=f"{type(exc).__name__}: {exc}")
            self.results.append(result)
            if result.status == OperationStatus.COMPLETED:
                return result
            if spec.failure_policy != "retry":
                break
        if spec.failure_policy == "record":
            return result
        raise StageFailed(result)

    def _usage(self, first_call):
        calls = self.actor_calls[first_call:]
        if not calls:
            return Usage(model_calls=0, input_tokens=0, output_tokens=0, cost_usd=0, wall_seconds=0)
        totals = {}
        for key in ("model_calls", "input_tokens", "output_tokens", "cost_usd"):
            values = [(call.get("result") or {}).get("usage", {}).get(key) for call in calls]
            totals[key] = None if any(value is None for value in values) else sum(values)
        return Usage(**totals, wall_seconds=sum(call["wall_seconds"] for call in calls))

    def create_candidate(self, artifact: ArtifactRef, *, candidate_id: str | None = None,
                         parents: tuple[Candidate, ...] = (), operation_id: str | None = None,
                         validation: ValidationStatus = ValidationStatus.UNCHECKED,
                         source_checkpoint: str | None = None) -> Candidate:
        return self._candidate(artifact, None, parents, candidate_id, operation_id, validation,
                               source_checkpoint)

    def initial_candidate(self, *, candidate_id: str = "initial") -> Candidate:
        require(self.recipe.subject is not None, "recipe has no subject")
        return self.create_candidate(self.recipe.subject.initial_artifact, candidate_id=candidate_id)

    def fork_candidate(self, parent: Candidate, *, delta: Contract | None = None,
                       candidate_id: str | None = None, operation_id: str | None = None,
                       validation: ValidationStatus = ValidationStatus.UNCHECKED) -> Candidate:
        return self._candidate(parent.artifact, delta, (parent,), candidate_id, operation_id,
                               validation, None)

    def _candidate(self, artifact, delta, parents, candidate_id, operation_id, validation, checkpoint):
        require(threading.get_ident() == self._dispatch_thread,
                "candidate operations require serial service dispatch")
        for parent in parents:
            require(parent.run_id == self.run_id
                    and parent in self.candidates
                    and self.artifacts.candidate(self.run_id, parent.id) == parent,
                    "candidate parent belongs to another run or differs from stored identity")
        operation_id = operation_id or f"op-{len(self._operations) + 1:06d}"
        require(operation_id not in self._operations, "operation already submitted")
        inputs = {"artifact": self.values.publish("proteus.artifact_ref@1", artifact)}
        inputs.update({f"parent-{i}": self.values.publish("proteus.candidate@1", parent)
                       for i, parent in enumerate(parents)})
        if delta is not None:
            require(isinstance(delta, Contract), "artifact delta must be a versioned Contract")
            inputs["delta"] = self.values.publish("proteus.artifact_proposal@1",
                ArtifactProposal(codec=artifact.codec, delta=delta.to_dict()))
        self._operations.add(operation_id)
        request = StageRequest(run_id=self.run_id, operation_id=operation_id, attempt_id="attempt-1",
            stage=PluginRef(name="proteus.artifact.candidate", version="1"), role="framework",
            inputs=inputs, read_scopes=tuple(sorted({ref.kind for ref in inputs.values()})),
            write_scopes=("proteus.candidate@1",), budget=BudgetLimits(max_model_calls=0))
        self.requests.append(request)
        started = time.monotonic()
        try:
            if delta is not None:
                artifact = self.artifacts.apply_delta(artifact, delta)
            candidate = Candidate(id=candidate_id or f"candidate-{len(self.candidates) + 1:06d}",
                run_id=self.run_id, artifact=artifact, created_by=operation_id,
                created_attempt_id=request.attempt_id, parent_ids=tuple(parent.id for parent in parents),
                validation=validation)
            self.artifacts.register_candidate(candidate, source_checkpoint=checkpoint)
            self.candidates.append(candidate)
            outputs = {"candidate": self.values.publish("proteus.candidate@1", candidate)}
        except Exception as exc:
            self.results.append(StageResult(run_id=self.run_id, operation_id=operation_id,
                attempt_id=request.attempt_id, status=OperationStatus.FAILED,
                usage=Usage(model_calls=0, input_tokens=0, output_tokens=0, cost_usd=0,
                            wall_seconds=time.monotonic() - started),
                diagnostic=f"{type(exc).__name__}: {exc}"))
            raise
        self.results.append(StageResult(run_id=self.run_id, operation_id=operation_id,
            attempt_id=request.attempt_id, status=OperationStatus.COMPLETED, outputs=outputs,
            usage=Usage(model_calls=0, input_tokens=0, output_tokens=0, cost_usd=0,
                        wall_seconds=time.monotonic() - started)))
        return candidate

    def open_evaluation(self, evaluation_id: str, *, operation_id: str | None = None):
        return self._evaluation_service.open(evaluation_id, operation_id=operation_id)

    def evaluate(self, candidate: Candidate, evaluation_id: str, *, operation_id: str | None = None):
        session = self.open_evaluation(evaluation_id, operation_id=operation_id)
        try:
            for _ in session.batch.tasks:
                session.step(candidate)
            return session.finish()
        except Exception:
            session.abort("candidate evaluation did not complete")
            raise
