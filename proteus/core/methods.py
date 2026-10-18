"""Synchronous method, stage and actor boundaries for the 1.0 plugin API.

Methods own control flow. Side-effecting actor work crosses a service boundary;
stage inputs/outputs are registered immutable records. Durable orchestration and
parallel dispatch are subsequent layers, not implicit behavior of this executor.
"""

from __future__ import annotations

import time
import threading
import random
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol

from proteus.core.contracts import (
    TEXT, ArtifactRef, BudgetLimits, Candidate, DataRef, EvaluationBatch, MethodState, OperationStatus, PluginRef,
    Rollout, RuntimeIdentity, StageRequest, StageResult, Usage, ValidationStatus, require, validate_records,
)
from proteus.core.recovery import RecoveryRequired, tuples
from proteus.core.serialization import Contract, content_hash
from proteus.recipes.config import ActorSpec, RecipeConfig
from proteus.execution.workspace_records import WorkspaceInput


@dataclass(frozen=True, kw_only=True)
class ActorRequest(Contract):
    prompt: str = field(metadata=TEXT)
    system: str = ""
    session_id: str | None = field(default=None, metadata=TEXT)
    handoff: str | None = None
    timeout_seconds: float | None = field(default=None, metadata={"exclusiveMinimum": 0})
    workspace: WorkspaceInput | None = None


@dataclass(frozen=True, kw_only=True)
class ActorResult(Contract):
    text: str
    runtime: RuntimeIdentity
    usage: Usage
    status: OperationStatus = OperationStatus.COMPLETED
    session_id: str | None = field(default=None, metadata=TEXT)
    trace: tuple[Mapping[str, Any], ...] = ()
    diagnostic: str = ""
    workspace_artifact: ArtifactRef | None = None


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
            "concurrent execution requires backend bounds/deadlines and the subsequent scheduler")


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

    def restore(self, records):
        self._values = {}
        for record in records:
            ref = DataRef.from_dict(record["ref"])
            value = self.registry.data_schema(ref.kind).from_dict(record["value"])
            require(self.publish(ref.kind, value) == ref, "restored data reference differs")


class StageContext:
    """A stage receives its bound actor and explicitly supplied typed inputs only.

    Scopes constrain data ports and tool declarations, not arbitrary Python/file access.
    Plugins are trusted code; process/container isolation belongs to execution backends.
    """

    def __init__(self, services, spec, request):
        self._services, self.spec, self.request = services, spec, request

    def actor(self, prompt: str, *, session_id: str | None = None,
              handoff: str | None = None, workspace: WorkspaceInput | None = None) -> ActorResult:
        return self._services._actor(self.spec, self.request, prompt, session_id, handoff, workspace_input=workspace)

    def read_evidence(self, view, path: str) -> bytes:
        evidence, ref = self._evidence(view)
        return evidence.store.read(ref, path)

    def search_evidence(self, view, text: str):
        evidence, ref = self._evidence(view)
        return evidence.store.search(ref, text)

    def materialize_evidence(self, view, destination: Path) -> Path:
        evidence, ref = self._evidence(view)
        return evidence.store.materialize(ref, destination)

    def _evidence(self, view):
        from proteus.evidence.service import EvidenceService
        evidence = EvidenceService(self._services)
        ref = evidence.bound(view, self.spec.id)
        require(ref in self.request.inputs.values(), "evidence was not supplied to this stage invocation")
        return evidence, ref


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
        self._ledger = None
        self._builds = None
        self._actor_ordinals = {}
        self._journal = None
        self.rng = random.Random(recipe.seed)
        for actor in recipe.actors.values():
            if actor.prompt_hash is not None:
                require(actor.prompt_hash in self.prompts, "role prompt content is missing")
                require(content_hash(self.prompts[actor.prompt_hash]) == actor.prompt_hash,
                        "role prompt content differs from its hash")

    @property
    def ledger(self):
        # Lazy creation preserves preflight failures without publishing a run.
        if self._ledger is None:
            from proteus.core.ledger import Ledger
            self._ledger = Ledger(self.root / "private" / "ledger.sqlite3", run_id=self.run_id,
                                  config_hash=self.recipe.config_hash, lock_hash=self.lock_hash)
        return self._ledger

    @property
    def builds(self):
        if self._builds is None:
            from proteus.execution.builds import BuildService
            self._builds = BuildService(self)
        return self._builds

    def snapshot(self):
        return {"run_id": self.run_id, "config_hash": self.recipe.config_hash, "lock_hash": self.lock_hash,
            "values": self.values.export(), "candidates": self.candidates, "rollouts": self.rollouts,
            "evaluations": self.evaluations, "requests": self.requests, "results": self.results,
            "actor_calls": self.actor_calls, "native_prompts": getattr(self, "native_prompts", []),
            "operations": sorted(self._operations), "actor_ordinals": [[key, value] for key, value in self._actor_ordinals.items()],
            "rng": self.rng.getstate(), "sessions": self._evaluation_service.snapshot(),
            "builds": self._builds.records if self._builds is not None else []}

    def restore(self, snapshot):
        require(snapshot["run_id"] == self.run_id and snapshot["config_hash"] == self.recipe.config_hash
                and snapshot["lock_hash"] == self.lock_hash, "service snapshot belongs to another experiment")
        self.values.restore(snapshot["values"])
        for name, schema in (("candidates", Candidate), ("rollouts", Rollout), ("evaluations", EvaluationBatch),
                             ("requests", StageRequest), ("results", StageResult)):
            setattr(self, name, [schema.from_dict(value) for value in snapshot[name]])
        validate_records(run_id=self.run_id, candidates=self.candidates, rollouts=self.rollouts,
                         evaluations=self.evaluations, requests=self.requests, results=self.results)
        for candidate in self.candidates:
            require(self.artifacts.candidate(self.run_id, candidate.id) == candidate, "restored candidate differs from store")
            self.artifacts.validate(candidate.artifact)
        self.actor_calls, self.native_prompts = snapshot["actor_calls"], snapshot["native_prompts"]
        from proteus.execution.workspace import WorkspaceService
        workspaces = WorkspaceService(self)
        for call in self.actor_calls:
            if call.get('result') is not None and not call.get('error'):
                workspaces.verify_result(ActorRequest.from_dict(call['request']).workspace,
                                         ActorResult.from_dict(call['result']))
        self._operations = set(snapshot["operations"])
        self._actor_ordinals = {tuple(key): value for key, value in snapshot["actor_ordinals"]}
        self.rng.setstate(tuples(snapshot["rng"]))
        self._evaluation_service.restore(snapshot["sessions"])
        if snapshot["builds"] or self._builds is not None:
            from proteus.execution.builds import BuildReceipt
            self.builds.records = [BuildReceipt.from_dict(value) for value in snapshot["builds"]]
        from proteus.evidence.service import EvidenceService
        evidence = EvidenceService(self)
        for ref, value in self.values._values.values():
            if ref.kind == "proteus.evidence.view@1":
                evidence.bound(value, value.stage_id, verify_files=True)

    def _record(self, kind, identity, function, *, decode, encode=lambda value: value.to_dict()):
        require(threading.get_ident() == self._dispatch_thread, "cross-thread dispatch requires serial service dispatch")
        if self._journal is None:
            return function()
        return self._journal.call(kind, identity, function, encode=encode, decode=decode)

    def checkpoint(self, payload, **kwargs):
        require(self._journal is not None, "checkpoints require a journal-enabled method")
        return self._journal.checkpoint(payload, **kwargs)

    def evidence_view(self, stage_id: str, *, candidate_ids: tuple[str, ...] | None = None) -> DataRef:
        from proteus.evidence.service import EvidenceService
        return EvidenceService(self).view(stage_id, candidate_ids=candidate_ids)

    def invoke_candidate(self, candidate: Candidate, call, *, operation_id: str | None = None):
        from proteus.execution.code_service import CodeService
        return CodeService(self).call(candidate, call, operation_id=operation_id)

    def evaluation_session(self, operation_id):
        session = next((item for item in self._evaluation_service.sessions if item.operation_id == operation_id), None)
        require(session is not None, "unknown evaluation session")
        return session

    def restore_trial_usage(self, trial_id):
        """Recover settled role records which preceded a missing prediction commit."""
        from proteus.execution.builds import BuildReceipt
        entries = {entry["id"]: entry for entry in self.ledger.entries()
                   if entry["identity"]["metadata"].get("trial_id") == trial_id}
        builds = []
        for event in self.ledger.events():
            if event["kind"] != "reserved" or event["entry"] not in entries:
                continue
            entry = entries[event["entry"]]
            if entry["settlement"] is None:
                continue
            metadata, record = entry["identity"]["metadata"], entry["settlement"]["result"]
            if metadata["kind"] == "actor" and record is not None:
                if not any(call["id"] == entry["id"] for call in self.actor_calls):
                    self.actor_calls.append(record)
                key = (metadata["operation_id"], metadata["attempt_id"], metadata["role"])
                self._actor_ordinals[key] = max(self._actor_ordinals.get(key, 0), metadata["ordinal"])
            elif metadata["kind"] == "source_build" and record is not None:
                receipt = BuildReceipt.from_dict(record["receipt"])
                if not any(old.id == receipt.id for old in self.builds.records):
                    self.builds.records.append(receipt)
                builds.append(receipt.usage)
        return builds

    def _actor(self, stage, operation, prompt, session_id, handoff, *, isolate=False, trial_id=None,
               timeout_seconds=None, workspace_input=None):
        from proteus.core.ledger import Amounts
        from proteus.execution.workspace import WorkspaceService
        require(threading.get_ident() == self._dispatch_thread,
                "P02 services are serial; cross-thread actor dispatch requires the parallel scheduler")
        spec = self.recipe.actors[stage.role]
        workspaces = WorkspaceService(self)
        if workspace_input is not None:
            require('actor.workspace' in self.registry.get(spec.runtime.plugin).capabilities,
                    'actor does not support explicit coding workspaces')
            require(spec.session.value == 'fresh' and session_id is None and handoff is None,
                    'coding workspaces currently require fresh actor sessions')
            workspaces.authorize(stage, operation, workspace_input)
        if timeout_seconds is not None:
            require("request.timeout" in self.registry.get(spec.runtime.plugin).capabilities,
                    "actor cannot honor a per-request transport timeout")
        if spec.session.value == "fresh":
            require(session_id is None and handoff is None, "fresh actors cannot inherit session state")
        elif spec.session.value == "handoff":
            require(session_id is None, "handoff is not a native session")
        else:
            require(handoff is None, "native sessions cannot silently become handoffs")
        request = ActorRequest(prompt=prompt, system=self.prompts.get(spec.prompt_hash, ""),
                               session_id=session_id, handoff=handoff, timeout_seconds=timeout_seconds,
                               workspace=workspace_input)
        if isolate:
            runtime = self.registry.create(spec.runtime, "actor")
        elif stage.role not in self._runtime:
            self._runtime[stage.role] = self.registry.create(spec.runtime, "actor")
        if not isolate:
            runtime = self._runtime[stage.role]
        bound = runtime.model_call_bound
        require(bound is None or (type(bound) is int and bound >= 0), "invalid actor call bound")
        limits = {"run": self.recipe.budget, f"role:{stage.role}": spec.budget,
                  f"stage:{operation.operation_id}": stage.budget}
        for key, budget in limits.items():
            supported_budget(budget, allow_evaluations=key == "run")
        key = (operation.operation_id, operation.attempt_id, stage.role)
        ordinal = self._actor_ordinals.get(key, 0) + 1
        call_id = "actor-" + content_hash([*key, ordinal]).split(":")[1]
        recovering = self._journal is not None and self._journal.recovering
        entry = self.ledger.reserve(call_id, scopes=limits, reservation=Amounts(model_calls=bound),
            metadata={"kind": "actor", "operation_id": operation.operation_id,
                      "attempt_id": operation.attempt_id, "stage": stage.id, "role": stage.role,
                      "trial_id": trial_id, "worker_id": None, "ordinal": ordinal},
            request={"actor": spec, "request": request}, reuse=recovering)
        self._actor_ordinals[key] = ordinal
        if recovering and entry["state"] != "reserved":
            if entry["state"] == "started":
                raise RecoveryRequired(f"actor {call_id} was dispatched without a settled result; reconcile before continuing")
            record = entry["settlement"]["result"]
            require(record is not None and record["id"] == call_id, "actor recovery record is unavailable")
            self.actor_calls.append(record)
            if record.get("error") or record["result"] is None:
                raise RuntimeError(record.get("error") or "previous actor call failed")
            result = ActorResult.from_dict(record["result"])
            workspaces.verify_result(workspace_input, result)
            return result
        workspace = self.root / "actors" / call_id
        record = {"id": call_id, "operation_id": operation.operation_id,
                  "attempt_id": operation.attempt_id, "role": stage.role,
                  "request": request.to_dict(), "prompt_hash": content_hash(request),
                  "reserved_model_calls": bound, "result": None}
        self.actor_calls.append(record)
        started = time.monotonic()
        result = None
        error = None
        try:
            workspace.mkdir(parents=True, exist_ok=recovering)
            if workspace_input is not None:
                workspaces.prepare(workspace_input, workspace, reserved_recovery=recovering)
            self.ledger.start(call_id)
            result = runtime.execute(spec, request, workspace)
            require(type(result) is ActorResult, "actor must return ActorResult")
            # Preserve returned usage before validating runtime identity or bounds.
            record["result"] = result.to_dict()
            require(result.runtime.runtime == spec.runtime.plugin, "actor returned a different runtime")
            actual = result.usage.model_calls
            require(bound is None or actual is None or actual <= bound,
                    "actor exceeded its declared model-call bound")
            if workspace_input is not None:
                result = workspaces.finish(workspace_input, workspace, result)
                record['result'] = result.to_dict()
            workspaces.verify_result(workspace_input, result)
            return result
        except BaseException as exc:
            error = record["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            record["wall_seconds"] = time.monotonic() - started
            if self.ledger.entry(call_id)["state"] == "reserved":
                record["usage"] = Usage(model_calls=0, input_tokens=0, output_tokens=0,
                                        cost_usd=0, wall_seconds=0).to_dict()
                self.ledger.settle(call_id, actual=Amounts.zero(), status="cancelled", result=record)
            else:
                usage = result.usage if type(result) is ActorResult else Usage()
                usage = replace(usage, wall_seconds=record["wall_seconds"])
                record["usage"] = usage.to_dict()
                self.ledger.settle(call_id,
                    actual=Amounts.from_usage(usage),
                    status="failed" if error or result.status != OperationStatus.COMPLETED else "completed",
                    result=record)

    def _check_operation(self, operation_id):
        require(operation_id not in self._operations, "operation already submitted")
        if self._ledger is not None or (self.root / "private/ledger.sqlite3").exists():
            require((self._journal is not None and self._journal.recovering) or not self.ledger.has_operation(operation_id),
                    "operation already submitted; durable replay/reconciliation is not enabled")

    def stage(self, stage_id: str, inputs: Mapping[str, DataRef], *, operation_id: str | None = None) -> StageResult:
        operation_id = operation_id or f"op-{len(self._operations) + 1:06d}"
        return self._record("stage", {"stage": stage_id, "inputs": inputs, "operation_id": operation_id},
            lambda: self._stage(stage_id, inputs, operation_id=operation_id), decode=StageResult.from_dict)

    def _stage(self, stage_id, inputs, *, operation_id):
        require(threading.get_ident() == self._dispatch_thread,
                "P02 services are serial; cross-thread stage dispatch requires the parallel scheduler")
        spec = next((item for item in self.recipe.stages if item.id == stage_id), None)
        require(spec is not None, f"unknown stage: {stage_id}")
        if (self._journal is not None and self._journal.recovering
                and "stage.replay" not in self.registry.get(spec.operator.plugin).capabilities):
            raise RecoveryRequired(f"stage {stage_id} does not declare safe replay of an interrupted invocation")
        require(set(inputs) == set(spec.input_types), f"{stage_id}: input port mismatch")
        values = {}
        for name, ref in inputs.items():
            require(ref.kind == spec.input_types[name], f"{stage_id}.{name}: input data kind mismatch")
            values[name] = self.values.get(ref)
            if ref.kind == "proteus.evidence.view@1":
                from proteus.evidence.service import EvidenceService
                EvidenceService(self).bound(values[name], stage_id, verify_files=True)
        operation_id = operation_id or f"op-{len(self._operations) + 1:06d}"
        self._check_operation(operation_id)
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
            except RecoveryRequired:
                raise
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
            values = [call.get("usage", (call.get("result") or {}).get("usage", {})).get(key)
                      for call in calls]
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
        operation_id = operation_id or f"op-{len(self._operations) + 1:06d}"
        return self._record("candidate", {"artifact": artifact, "delta": delta, "parents": parents,
            "candidate_id": candidate_id, "operation_id": operation_id, "validation": validation, "checkpoint": checkpoint},
            lambda: self._create_candidate(artifact, delta, parents, candidate_id, operation_id, validation, checkpoint),
            decode=Candidate.from_dict)

    def _create_candidate(self, artifact, delta, parents, candidate_id, operation_id, validation, checkpoint):
        require(threading.get_ident() == self._dispatch_thread,
                "candidate operations require serial service dispatch")
        for parent in parents:
            require(parent.run_id == self.run_id
                    and parent in self.candidates
                    and self.artifacts.candidate(self.run_id, parent.id) == parent,
                    "candidate parent belongs to another run or differs from stored identity")
        operation_id = operation_id or f"op-{len(self._operations) + 1:06d}"
        self._check_operation(operation_id)
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

    def task_pool(self, evaluation_id: str):
        return self._evaluation_service.task_pool(evaluation_id)

    def archive_snapshot(self):
        from proteus.core.search import SearchService
        return SearchService(self).snapshot()

    def selection_view(self, archive, evaluation_id, *, protocol_hash=None, objectives=(), candidate_ids=None):
        from proteus.core.search import SearchService
        return SearchService(self).view(archive, evaluation_id, protocol_hash=protocol_hash,
                                        objectives=objectives, candidate_ids=candidate_ids)

    def select_parents(self, policy, view, *, operation_id=None):
        from proteus.core.search import SearchService
        return SearchService(self).choose("parent", policy, view, operation_id=operation_id)

    def retain_candidates(self, policy, view, *, operation_id=None):
        from proteus.core.search import SearchService
        return SearchService(self).choose("archive", policy, view, operation_id=operation_id)

    def select_final(self, policy, view, *, operation_id=None):
        from proteus.core.search import SearchService
        return SearchService(self).choose("final", policy, view, operation_id=operation_id)

    def open_evaluation(self, evaluation_id: str, *, operation_id: str | None = None, selection=None, inputs=None):
        return self._evaluation_service.open(evaluation_id, operation_id=operation_id, selection=selection, inputs=inputs)

    def evaluate(self, candidate: Candidate, evaluation_id: str, *, operation_id: str | None = None, selection=None, inputs=None):
        session = self.open_evaluation(evaluation_id, operation_id=operation_id, selection=selection, inputs=inputs)
        try:
            for _ in session.batch.tasks:
                session.step(candidate)
            return session.finish()
        except RecoveryRequired:
            raise
        except Exception:
            session.abort("candidate evaluation did not complete")
            raise
