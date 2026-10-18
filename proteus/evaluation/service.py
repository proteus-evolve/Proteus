"""Serial evaluation sessions with prediction-before-feedback publication.

Each session commits its complete declared task cohort. Methods may replace the
candidate between steps for online learning; per-candidate batches retain their
actual task membership. Hidden grades stay in private records and never enter the
public Values store or the subject's history. Plugins remain trusted Python code.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from dataclasses import replace
from types import MappingProxyType

from proteus.artifacts.store import publish_bytes
from proteus.core.contracts import (
    Candidate, DataRef, EvaluationBatch, EvaluationRow, FeedbackVisibility, OperationStatus, Outcome,
    PluginRef, Rollout, RuntimeIdentity, StageRequest, StageResult, StatePolicy, Usage, ValidationStatus, require,
)
from proteus.core.serialization import canonical_json, content_hash
from proteus.core.ledger import Amounts
from proteus.core.recovery import RecoveryRequired
from proteus.evaluation.records import (
    BackendEvaluation, EvaluationReceipt, Grade, Observation, StateUpdate, SubjectResult, TrialRecord,
    sum_usage, zero_usage,
)
from proteus.evaluation.runners import EnvironmentLease, InvalidCandidate, SubjectContext, SubjectRecoveryContext
from proteus.evaluation.tasks import StateValue, TaskBatch, make_batch
from proteus.recipes.config import StageSpec


VALID = (Outcome.SUCCEEDED, Outcome.TASK_FAILED)


def validate_evaluation_config(recipe, registry):
    if not recipe.evaluations:
        return
    require(recipe.subject is not None and recipe.execution is not None,
            "formal evaluation requires a subject and execution specification")
    require(recipe.execution.state_policy != StatePolicy.WORKER,
            "persistent worker evaluation requires the later worker/session service")
    environment = registry.get(recipe.execution.environment.backend.plugin, "environment")
    require(f"state.{recipe.execution.state_policy.value}" in environment.capabilities,
            "environment does not support the requested state policy")
    for spec in recipe.evaluations:
        require(spec.tasks.dataset_hash is not None, "evaluations require a pinned task dataset_hash")
        capabilities = registry.get(spec.evaluator.plugin, "evaluator").capabilities
        backend = {cap for cap in capabilities if cap.startswith("evaluation.backend.")}
        if backend:
            subject_caps = registry.get(recipe.subject.runner.plugin, "subject").capabilities
            require(backend.issubset(subject_caps), "backend evaluator requires a matching subject adapter")
            require(recipe.budget.max_model_calls is None,
                    "integrated verifier cannot enforce a finite model-call cap")
        else:
            require("evaluator.deterministic" in capabilities,
                    "evaluators must be deterministic or a declared integrated backend")
    subject = recipe.subject
    subject_plugin = registry.get(subject.runner.plugin, "subject")
    require("subject.reconcile" not in subject_plugin.capabilities or subject.actor is None,
            "subject reconciliation currently requires a self-contained runner, not delegated actor calls")
    if recipe.budget.max_model_calls is not None:
        require("subject.managed_actor" in subject_plugin.capabilities
                or "subject.model_free" in subject_plugin.capabilities,
                "subject cannot enforce a finite model-call cap")
    if subject.actor is not None:
        actor = recipe.actors[subject.actor]
        plugin = registry.get(actor.runtime.plugin, "actor")
        require("actor.execute" in plugin.capabilities, "subject actor lacks generic execution")
        if recipe.budget.max_model_calls is not None or actor.budget.max_model_calls is not None:
            require("calls.bounded" in plugin.capabilities, "subject actor cannot enforce a finite model-call cap")
    if subject.initial_artifact.runtime is not None:
        expected = recipe.actors[subject.actor].runtime.plugin if subject.actor else subject.runner.plugin
        require(subject.initial_artifact.runtime == expected, "artifact runtime differs from subject binding")
    if subject.initial_artifact.environment_hash is not None:
        require(subject.initial_artifact.environment_hash == content_hash(recipe.execution.environment),
                "artifact environment differs from execution binding")


def cache_key(*, candidate, task, batch, subject, actors, evaluation, execution, state, history,
              position, seed, lock_hash, implementations, inputs=None):
    """An identity definition, not a cache hit/reuse policy. P03 always executes."""
    return content_hash({"artifact": candidate.artifact, "task": task, "dataset": batch.dataset_hash,
        "subject": subject, "actors": actors, "evaluation": evaluation, "execution": execution,
        "initial_state": batch.initial_state, "state": state, "history": history,
        "position": position, "seed": seed, "lock_hash": lock_hash, "implementations": implementations,
        "inputs": inputs or {}})


class EvaluationService:
    def __init__(self, services):
        self.services = services
        self.sessions = []

    def snapshot(self):
        return [session.snapshot() for session in self.sessions]

    def restore(self, records):
        old = {session.operation_id: session for session in self.sessions}
        self.sessions = []
        for record in records:
            session = old.get(record["operation_id"]) or object.__new__(EvaluationSession)
            session.restore(self, record)
            self.sessions.append(session)

    @property
    def reserved(self):
        if self.services._ledger is None and not (self.services.root / "private/ledger.sqlite3").exists():
            return 0
        return self.services.ledger.balance()["charged"]["evaluations"]

    def _load_pool(self, evaluation_id):
        validate_evaluation_config(self.services.recipe, self.services.registry)
        spec = next((spec for spec in self.services.recipe.evaluations if spec.id == evaluation_id), None)
        require(spec is not None, f"unknown declared evaluation: {evaluation_id}")
        dataset = self.services.registry.create(spec.tasks.suite, "task_suite").load()
        return spec, dataset

    def task_pool(self, evaluation_id):
        def load():
            spec, dataset = self._load_pool(evaluation_id)
            return make_batch(dataset, spec.tasks)
        return self.services._record("evaluation.pool", {"evaluation_id": evaluation_id}, load,
                                     decode=TaskBatch.from_dict)

    def open(self, evaluation_id, *, operation_id=None, selection=None, inputs=None):
        inputs = {} if inputs is None else inputs
        self.input_values(inputs)
        inputs = dict(inputs)
        operation_id = operation_id or f"op-{len(self.services._operations) + 1:06d}"
        return self.services._record("evaluation.open", {"evaluation_id": evaluation_id,
            "operation_id": operation_id, "selection": selection, "inputs": inputs},
            lambda: self._open(evaluation_id, operation_id=operation_id, selection=selection, inputs=inputs), encode=lambda session: session.operation_id,
            decode=self.services.evaluation_session)

    def input_values(self, inputs):
        subject = self.services.recipe.subject
        require(subject is not None, "evaluation requires a subject")
        ports = self.services.registry.get(subject.runner.plugin, "subject").inputs
        require(isinstance(inputs, Mapping) and set(inputs) == set(ports),
                "evaluation inputs must match the subject's declared ports")
        values = {}
        for name, ref in inputs.items():
            require(type(ref) is DataRef and ref.kind == ports[name], "subject input has the wrong data kind")
            values[name] = self.services.values.get(ref)
        return MappingProxyType(values)

    def _open(self, evaluation_id, *, operation_id, selection, inputs):
        services = self.services
        require(threading.get_ident() == services._dispatch_thread, "evaluation requires serial service dispatch")
        require(not services.resume or services._journal is not None, "evaluation recovery requires the durable operation journal")
        spec, dataset = self._load_pool(evaluation_id)
        batch = make_batch(dataset, spec.tasks, selection)
        execution = services.recipe.execution
        require(execution.initial_state_hash is None or execution.initial_state_hash == content_hash(batch.initial_state),
                "initial state differs from ExecutionSpec")
        operation_id = operation_id or f"op-{len(services._operations) + 1:06d}"
        services._check_operation(operation_id)
        entry_id = "evaluation-" + content_hash(operation_id).split(":")[1]
        recovering = services._journal is not None and services._journal.recovering
        entry = services.ledger.reserve(entry_id, scopes={"run": services.recipe.budget},
            reservation=Amounts.zero(evaluations=len(batch.tasks)), slots=0,
            metadata={"kind": "evaluation_cohort", "operation_id": operation_id,
                      "attempt_id": "attempt-1", "stage": "evaluation", "role": "framework",
                      "trial_id": None, "worker_id": None},
            request={"evaluation": spec, "batch": batch, "execution": execution, "inputs": inputs}, reuse=recovering)
        if entry["state"] == "reserved":
            services.ledger.start(entry_id)
        elif entry["state"] != "started":
            raise RecoveryRequired(f"evaluation cohort {entry_id} already settled without a committed session")
        try:
            session = EvaluationSession(self, spec, dataset, batch, operation_id, entry_id, inputs)
        except BaseException as exc:
            services.ledger.settle(entry_id, actual=Amounts.zero(evaluations=len(batch.tasks)),
                status="failed", result={"error": f"{type(exc).__name__}: {exc}"})
            raise
        services._operations.add(operation_id)
        self.sessions.append(session)
        return session

    def finalize_incomplete(self):
        for session in self.sessions:
            if not session.closed:
                session.abort("method returned before completing its declared evaluation cohort")


class EvaluationSession:
    def __init__(self, owner, spec, dataset, batch, operation_id, entry_id, inputs):
        self.owner, self.services, self.spec, self.batch = owner, owner.services, spec, batch
        self.operation_id = operation_id
        self.ledger_entry = entry_id
        self.input_refs = MappingProxyType(dict(inputs))
        self.closed, self._running, self._sequence_failed = False, False, False
        self._observations, self._grades, self._grade_usage, self._trials = [], {}, {}, []
        self._backend_evaluations, self._backend_usage = {}, {}
        self._build_usage = []
        self._updates = []
        self._pending_usage = self._pending_grade_usage = None
        self._state = batch.initial_state
        self._references = {record.task.id: record.reference for record in dataset.records
                            if record.task.split == spec.tasks.split}
        self.root = self.services.root / "evaluations" / content_hash(operation_id).split(":")[1]
        publish_bytes(self.services.root / "private" / "datasets" / (batch.dataset_hash.split(":")[1] + ".json"),
                      canonical_json(dataset).encode("utf-8"))
        self._save("private/dataset-ref.json", {"dataset_hash": batch.dataset_hash})
        self._save("protocol.json", {"evaluation": spec, "batch": batch,
            "execution": self.services.recipe.execution, "inputs": self.input_refs,
            "reserved_evaluations": len(batch.tasks)})
        self.request = StageRequest(run_id=self.services.run_id, operation_id=operation_id, attempt_id="attempt-1",
            stage=PluginRef(name="proteus.evaluation", version="1"), role="framework",
            inputs={"tasks": self._value("proteus.eval.task_batch@1", batch),
                    **{"subject." + name: ref for name, ref in self.input_refs.items()}},
            read_scopes=tuple(sorted({"proteus.candidate@1", "proteus.eval.task_batch@1",
                                     *(ref.kind for ref in self.input_refs.values())})),
            write_scopes=("proteus.eval.receipt@1",), budget=self.services.recipe.budget)
        self.services.requests.append(self.request)
        self.protocol_hash = content_hash({"spec": spec, "batch": batch,
            "subject": self.services.recipe.subject, "execution": self.services.recipe.execution,
            "lock_hash": self.services.lock_hash, "inputs": self.input_refs})

    def snapshot(self):
        return {"evaluation_id": self.spec.id, "operation_id": self.operation_id, "batch": self.batch,
            "ledger_entry": self.ledger_entry, "closed": self.closed, "sequence_failed": self._sequence_failed,
            "observations": self._observations, "grades": {str(k): v for k, v in self._grades.items()},
            "grade_usage": {str(k): v for k, v in self._grade_usage.items()}, "trials": self._trials,
            "backend_evaluations": {str(k): v for k, v in self._backend_evaluations.items()},
            "backend_usage": {str(k): v for k, v in self._backend_usage.items()}, "build_usage": self._build_usage,
            "updates": self._updates, "pending_usage": self._pending_usage, "pending_grade_usage": self._pending_grade_usage,
            "state": self._state, "references": self._references, "request": self.request, "protocol_hash": self.protocol_hash,
            "inputs": self.input_refs}

    def restore(self, owner, record):
        self.owner, self.services = owner, owner.services
        self.operation_id, self.ledger_entry = record["operation_id"], record["ledger_entry"]
        self.input_refs = MappingProxyType({name: DataRef.from_dict(ref) for name, ref in record["inputs"].items()})
        owner.input_values(self.input_refs)
        self.spec = next(spec for spec in self.services.recipe.evaluations if spec.id == record["evaluation_id"])
        self.batch = TaskBatch.from_dict(record["batch"])
        self.closed, self._sequence_failed, self._running = record["closed"], record["sequence_failed"], False
        for name, schema in (("observations", Observation), ("trials", TrialRecord), ("updates", StateUpdate), ("build_usage", Usage)):
            setattr(self, "_" + name, [schema.from_dict(item) for item in record[name]])
        for name, schema in (("grades", EvaluationRow), ("grade_usage", Usage),
                             ("backend_evaluations", BackendEvaluation), ("backend_usage", Usage)):
            setattr(self, "_" + name, {int(key): schema.from_dict(value) for key, value in record[name].items()})
        for name in ("pending_usage", "pending_grade_usage"):
            setattr(self, "_" + name, Usage.from_dict(record[name]) if record[name] is not None else None)
        self._state, self._references = StateValue.from_dict(record["state"]), record["references"]
        self.request = StageRequest.from_dict(record["request"])
        self.protocol_hash = record["protocol_hash"]
        self.root = self.services.root / "evaluations" / content_hash(self.operation_id).split(":")[1]

    @property
    def position(self):
        return len(self._observations)

    def _save(self, name, value):
        publish_bytes(self.root / name, canonical_json(value).encode("utf-8"))

    def _value(self, kind, value, *, private=False):
        if private:
            digest = content_hash(value)
            ref = DataRef(kind=kind, id=content_hash([kind, digest]), content_hash=digest)
        else:
            ref = self.services.values.publish(kind, value)
        prefix = "private/values" if private else "values"
        self._save(f"{prefix}/{ref.id.split(':')[1]}.json", {"ref": ref, "value": value})
        return ref

    def _check(self):
        require(threading.get_ident() == self.services._dispatch_thread, "evaluation requires serial service dispatch")
        require(not self.closed and not self._running, "evaluation session is closed or already executing")

    def _backend(self):
        return any(cap.startswith("evaluation.backend.") for cap in
                   self.services.registry.get(self.spec.evaluator.plugin).capabilities)

    def _reserve_dispatch(self, kind, index, *, request, reservation, slots):
        entry_id = kind + "-" + content_hash([self.operation_id, self.request.attempt_id, index]).split(":")[1]
        self.services.ledger.reserve(entry_id, scopes={"run": self.services.recipe.budget},
            reservation=reservation, slots=slots,
            reuse=self.services._journal is not None and self.services._journal.recovering,
            metadata={"kind": kind, "operation_id": self.operation_id, "attempt_id": self.request.attempt_id,
                      "role": "grader" if kind == "backend_verifier" else kind,
                      "trial_id": f"{self.operation_id}:rollout-{index + 1}", "worker_id": None}, request=request)
        return entry_id

    def step(self, candidate: Candidate) -> Observation:
        return self.services._record("evaluation.step", {"operation_id": self.operation_id,
            "position": self.position, "candidate": candidate}, lambda: self._step(candidate), decode=Observation.from_dict)

    def _step(self, candidate):
        self._check()
        index = len(self._observations)
        require(index < len(self.batch.tasks), "evaluation cohort has no remaining task")
        require(candidate.run_id == self.services.run_id and candidate in self.services.candidates,
                "evaluation requires a candidate created by this run's services")
        self._running = True
        try:
            observation = self._predict(candidate, index)
            # Publication precedes grading and returning an observation to the method.
            self._observations.append(observation)
            if self.spec.feedback_timing == "after_prediction":
                observation = self._grade(index)
                self._observations[index] = observation
            return observation
        except RecoveryRequired:
            raise
        except BaseException as exc:
            if self.services._journal is not None and not isinstance(exc, Exception):
                raise
            self.abort("prediction/feedback step did not commit; automatic replay is unavailable")
            raise
        finally:
            self._running = False

    def _predict(self, candidate, index):
        services, recipe = self.services, self.services.recipe
        task = self.batch.tasks[index]
        sequence = recipe.execution.state_policy == StatePolicy.SEQUENCE
        state = self._state if sequence else self.batch.initial_state
        history = tuple(self._observations) if sequence else ()
        seed = int(content_hash([recipe.execution.seed, self.batch.seed, index, task.id]).split(":")[1][:15], 16)
        state_ref = self._value("proteus.eval.state@1", state)
        invocation_inputs = self.owner.input_values(self.input_refs)
        self._pending_usage = Usage()
        started = time.monotonic()
        runtime_ref = recipe.actors[recipe.subject.actor].runtime.plugin if recipe.subject.actor else recipe.subject.runner.plugin
        runtime = RuntimeIdentity(runtime=runtime_ref, environment_hash=content_hash(recipe.execution.environment))
        first_actor = len(services.actor_calls)
        managed = recipe.subject.actor is not None
        model_free = "subject.model_free" in services.registry.get(recipe.subject.runner.plugin).capabilities
        subject_entry = self._reserve_dispatch("subject", index,
            request={"candidate": candidate, "task": task, "execution": recipe.execution,
                     "state": state, "history_hash": content_hash(history), "seed": seed,
                     "delegates_usage_to_actor": managed, "inputs": self.input_refs},
            reservation=Amounts.zero() if managed else Amounts(model_calls=0 if model_free else None),
            slots=0 if managed else 1)
        verifier_entry = None
        if self._backend():
            try:
                verifier_entry = self._reserve_dispatch("backend_verifier", index,
                    request={"candidate": candidate, "task": task, "evaluation": self.spec, "inputs": self.input_refs},
                    reservation=Amounts(), slots=0)
            except BaseException:
                services.ledger.settle(subject_entry, actual=Amounts.zero(), status="cancelled")
                raise
        subject_record = services.ledger.entry(subject_entry)
        replayed, reconciled = None, None
        if subject_record["state"] == "reserved":
            services.ledger.start(subject_entry)
        else:
            prior = subject_record["settlement"]
            if prior is None or not (prior["result"] or {}).get("subject_result"):
                definition = services.registry.get(recipe.subject.runner.plugin)
                if subject_record["state"] != "started" or "subject.reconcile" not in definition.capabilities:
                    raise RecoveryRequired(f"subject {subject_entry} has no settled execution result; reconcile before continuing")
                context = SubjectRecoveryContext(candidate=candidate, task=task, state=state, history=history,
                    seed=seed, execution=recipe.execution, subject=recipe.subject, inputs=invocation_inputs,
                    root=self.root / "trials" / f"{index:06d}")
                try:
                    reconciled = services.registry.create(recipe.subject.runner, "subject").reconcile(context)
                    self._validate_subject_result(reconciled, candidate, task, runtime)
                    self._save(f"private/reconciled/{index:06d}.json", {"entry_id": subject_entry,
                        "runner": recipe.subject.runner.plugin, "result_hash": content_hash(reconciled)})
                except Exception as exc:
                    # Invalid/missing evidence is uncertainty, not a failed trial
                    # or permission to repeat a dispatch. Retain every reservation.
                    raise RecoveryRequired(f"subject {subject_entry}: {type(exc).__name__}: {exc}") from exc
            else:
                replayed = prior["result"]
        if verifier_entry is not None:
            if services.ledger.entry(verifier_entry)["state"] == "reserved":
                services.ledger.start(verifier_entry)
        result = None
        rejected_result = None
        subject_dispatched = False
        build_elapsed = 0.0
        def build(artifact, spec, base_image):
            nonlocal build_elapsed
            first = len(services.builds.records)
            before = time.monotonic()
            try:
                return services.builds.execute(artifact, spec, base_image, operation_id=self.operation_id,
                    attempt_id=self.request.attempt_id, trial_id=f"{self.operation_id}:rollout-{index + 1}")
            finally:
                build_elapsed += time.monotonic() - before
                self._build_usage.extend(record.usage for record in services.builds.records[first:])
        try:
            if replayed is not None:
                result = SubjectResult.from_dict(replayed["subject_result"])
                subject_dispatched = replayed["subject_dispatched"]
                rejected_result = SubjectResult.from_dict(replayed["rejected_result"]) if replayed["rejected_result"] else None
                self._build_usage.extend(services.restore_trial_usage(f"{self.operation_id}:rollout-{index + 1}"))
            elif reconciled is not None:
                result, subject_dispatched = reconciled, True
                self._build_usage.extend(services.restore_trial_usage(f"{self.operation_id}:rollout-{index + 1}"))
            elif self._sequence_failed:
                result = SubjectResult(loaded_artifact_hash=candidate.artifact.content_hash, runtime=runtime,
                    status=Outcome.CANCELLED, state=state, usage=zero_usage(),
                    diagnostic="prior sequence state is unavailable")
            elif candidate.validation == ValidationStatus.INVALID:
                result = SubjectResult(loaded_artifact_hash=candidate.artifact.content_hash, runtime=runtime,
                    status=Outcome.INVALID_CANDIDATE, state=state, usage=zero_usage(),
                    diagnostic="candidate is marked invalid")
            elif ((candidate.artifact.runtime is not None and candidate.artifact.runtime != runtime_ref)
                  or (candidate.artifact.environment_hash is not None
                      and candidate.artifact.environment_hash != runtime.environment_hash)):
                result = SubjectResult(loaded_artifact_hash=candidate.artifact.content_hash, runtime=runtime,
                    status=Outcome.INVALID_CANDIDATE, state=state, usage=zero_usage(),
                    diagnostic="candidate is bound to a different runtime/environment")
            else:
                environment = services.registry.create(recipe.execution.environment.backend, "environment")
                options = {"build": build} if "environment.source_build" in services.registry.get(
                    recipe.execution.environment.backend.plugin).capabilities else {}
                with environment.open(candidate.artifact, services.artifacts,
                        self.root / "trials" / f"{index:06d}", recipe.execution, **options) as lease:
                    require(isinstance(lease, EnvironmentLease)
                            and lease.loaded_artifact_hash == candidate.artifact.content_hash,
                            "environment loaded a different candidate")
                    actor = None
                    if recipe.subject.actor is not None:
                        stage = StageSpec(id="subject", operator=recipe.subject.runner, role=recipe.subject.actor)
                        def actor(prompt):
                            return services._actor(stage, self.request, prompt, None, None, isolate=True,
                                trial_id=f"{self.operation_id}:rollout-{index + 1}")
                    context = SubjectContext(candidate=candidate, task=task, state=state, history=history,
                        seed=seed, execution=recipe.execution, subject=recipe.subject, lease=lease, _actor=actor,
                        inputs=invocation_inputs)
                    # New runner instance per task prevents undeclared plugin-instance memory.
                    subject_dispatched = True
                    result = services.registry.create(recipe.subject.runner, "subject").run(context)
                self._validate_subject_result(result, candidate, task, runtime)
                if recipe.subject.actor is not None:
                    usage = services._usage(first_actor)
                    require(result.usage.model_calls == usage.model_calls, "subject actor usage mismatch")
                    result = replace(result, usage=usage)
        except RecoveryRequired:
            raise
        except Exception as exc:
            rejected_result = result if type(result) is SubjectResult else None
            fallback_usage = Usage() if subject_dispatched else zero_usage()
            usage = services._usage(first_actor) if len(services.actor_calls) > first_actor else (
                result.usage if type(result) is SubjectResult else fallback_usage)
            result = SubjectResult(loaded_artifact_hash=candidate.artifact.content_hash, runtime=runtime,
                status=Outcome.INVALID_CANDIDATE if isinstance(exc, InvalidCandidate) else Outcome.INFRA_ERROR,
                state=state, usage=usage,
                diagnostic=f"{type(exc).__name__}: {exc}")
        finally:
            if replayed is None and reconciled is None and type(result) is SubjectResult and result.backend_evaluation is None:
                result = replace(result, usage=replace(result.usage,
                    wall_seconds=max(0, time.monotonic() - started - build_elapsed)))
            fallback_usage = Usage() if subject_dispatched else zero_usage()
            observed = result.usage if type(result) is SubjectResult else fallback_usage
            self._pending_usage = observed
            services.ledger.settle(subject_entry, actual=Amounts.zero() if managed else Amounts.from_usage(observed),
                status="completed" if type(result) is SubjectResult and result.status in VALID else "failed",
                result={"usage": observed, "delegates_usage_to_actor": managed,
                        "status": result.status if type(result) is SubjectResult else "interrupted",
                        "rejected_result": rejected_result, "subject_result": result,
                        "subject_dispatched": subject_dispatched})
            if verifier_entry is not None:
                evidence_result = rejected_result or result
                backend = evidence_result.backend_evaluation if type(evidence_result) is SubjectResult else None
                self._backend_usage[index] = backend.usage if backend else fallback_usage
                services.ledger.settle(verifier_entry, actual=Amounts.from_usage(self._backend_usage[index]),
                    status="completed" if backend and rejected_result is None else "failed",
                    result={"backend_evidence": backend})
        after = result.state if result.status in VALID else state
        if result.backend_evaluation is not None:
            self._save(f"private/backend/{index:06d}.json", result.backend_evaluation)
            self._backend_evaluations[index] = result.backend_evaluation
        # Integrated backends report agent and verifier phase durations separately.
        usage = result.usage
        self._pending_usage = usage
        rollout = Rollout(id=f"{self.operation_id}:rollout-{index + 1}", run_id=services.run_id,
            operation_id=self.operation_id, attempt_id=self.request.attempt_id,
            candidate_id=candidate.id, artifact_hash=candidate.artifact.content_hash,
            task_id=task.id, task_version=task.version, split=task.split,
            execution_hash=content_hash({"execution": recipe.execution, "inputs": self.input_refs})
                if self.input_refs else content_hash(recipe.execution), runtime=result.runtime, status=result.status,
            usage=usage, output=result.output,
            trace=(self._value("proteus.eval.trace@1", result.trace),),
            sequence_id=self.operation_id if sequence else None, step=index if sequence else None,
            state_before=state_ref, state_after=self._value("proteus.eval.state@1", after),
            diagnostic=result.diagnostic)
        implementations = {ref.name + "@" + ref.version: services.registry.get(ref).implementation_hash
                           for ref in recipe.plugin_refs()}
        key = cache_key(candidate=candidate, task=task, batch=self.batch, subject=recipe.subject,
            actors=recipe.actors, evaluation=self.spec, execution=recipe.execution, state=state,
            history=history, position=index, seed=seed, lock_hash=services.lock_hash, implementations=implementations,
            inputs=self.input_refs)
        trial = TrialRecord(rollout=rollout, cache_key=key, task_hash=content_hash(task),
                            history_hash=content_hash(history), task_seed=seed, inputs=self.input_refs)
        self._save(f"predictions/{index:06d}.json", trial)
        if sequence:
            self._state = after
            self._sequence_failed = self._sequence_failed or result.status not in VALID
        self._trials.append(trial)
        services.rollouts.append(rollout)
        self._pending_usage = None
        return Observation(rollout=rollout)

    def _validate_subject_result(self, result, candidate, task, runtime):
        recipe = self.services.recipe
        require(type(result) is SubjectResult, "subject must return SubjectResult")
        require(result.loaded_artifact_hash == candidate.artifact.content_hash,
                "subject reported a different loaded candidate")
        require(result.runtime.runtime == runtime.runtime
                and result.runtime.environment_hash == runtime.environment_hash,
                "subject runtime/environment differs from configured execution")
        if result.backend_evaluation is not None:
            backend = result.backend_evaluation
            require(backend.producer == recipe.subject.runner.plugin
                    and backend.artifact_hash == candidate.artifact.content_hash
                    and backend.task_hash == content_hash(task),
                    "backend evidence belongs to a different producer/candidate/task")
            require(self._backend(), "backend evidence requires its declared evaluator")
        if "subject.model_free" in self.services.registry.get(recipe.subject.runner.plugin).capabilities:
            require(result.usage.model_calls == 0, "model-free subject reported nonzero/unknown model calls")

    def _grade(self, index):
        observation = self._observations[index]
        rollout = observation.rollout
        if index in self._grades:
            return observation
        started = time.monotonic()
        self._pending_grade_usage = Usage()
        backend = self._backend_evaluations.get(index)
        is_backend = self._backend()
        if is_backend:
            self._pending_grade_usage = None  # Already tracked when the trial ran.
        # Integrated verifier usage was reserved before the external trial and
        # settled with its private result. Only ordinary graders dispatch here.
        grader_entry = None
        replayed_grade = None
        if not is_backend:
            grader_entry = self._reserve_dispatch("grader", index,
                request={"rollout": rollout, "evaluation": self.spec, "reference": self._references[rollout.task_id]},
                reservation=Amounts(model_calls=0), slots=1)
            entry = self.services.ledger.entry(grader_entry)
            if entry["state"] == "reserved":
                self.services.ledger.start(grader_entry)
            elif entry["settlement"] is not None and entry["settlement"]["result"] is not None:
                replayed_grade = Grade.from_dict(entry["settlement"]["result"]["grade"])
            else:
                raise RecoveryRequired(f"grader {grader_entry} has no settled result; reconcile before continuing")
        if replayed_grade is not None:
            grade = replayed_grade
        elif rollout.status not in VALID:
            grade = Grade(status=rollout.status, score=None,
                usage=self._backend_usage[index] if is_backend else zero_usage(), diagnostic=rollout.diagnostic)
        else:
            grade = None
            try:
                grader = self.services.registry.create(self.spec.evaluator, "evaluator")
                if is_backend:
                    require(backend is not None, "integrated evaluator has no private backend evidence")
                    grade = grader.grade(self.batch.tasks[index], rollout,
                        self._references[rollout.task_id], backend=backend)
                    require(type(grade) is Grade and grade.usage == backend.usage,
                            "backend evaluator must preserve the recorded verifier usage")
                else:
                    grade = grader.grade(self.batch.tasks[index], rollout, self._references[rollout.task_id])
                    require(type(grade) is Grade and grade.usage.model_calls == 0,
                            "deterministic evaluator must return Grade with zero model calls")
            except (Exception, SystemExit) as exc:
                grade = Grade(status=Outcome.INFRA_ERROR, score=None,
                              usage=self._backend_usage[index] if is_backend else (
                                  grade.usage if type(grade) is Grade else Usage()),
                              diagnostic=f"{type(exc).__name__}: {exc}")
        if not is_backend:
            if replayed_grade is None:
                grade = replace(grade, usage=replace(grade.usage, wall_seconds=time.monotonic() - started))
            self.services.ledger.settle(grader_entry, actual=Amounts.from_usage(grade.usage),
                status="completed" if grade.status in VALID else "failed", result={"grade": grade})
        self._pending_grade_usage = None if is_backend else grade.usage
        hidden = self.spec.visibility == FeedbackVisibility.HIDDEN
        feedback = self._value("proteus.eval.feedback@1", grade.feedback, private=hidden)
        row = EvaluationRow(rollout_id=rollout.id, task_id=rollout.task_id, status=grade.status,
                            score=grade.score, metrics=grade.metrics, feedback=(feedback,), diagnostic=grade.diagnostic)
        self._save(f"private/grades/{index:06d}.json", {"row": row, "grade": grade})
        self._grades[index], self._grade_usage[index] = row, grade.usage
        self._pending_grade_usage = None
        return observation if hidden else Observation(rollout=rollout, row=row, feedback=grade.feedback)

    def update_state(self, state: StateValue):
        return self.services._record("evaluation.state", {"operation_id": self.operation_id,
            "position": self.position, "state": state}, lambda: self._update_state(state), encode=lambda value: None, decode=lambda value: None)

    def _update_state(self, state):
        """Explicit online state update after committed, visible prediction feedback."""
        self._check()
        require(self.services.recipe.execution.state_policy == StatePolicy.SEQUENCE
                and self.spec.feedback_timing == "after_prediction" and self._observations,
                "state updates require sequence state and previously released prediction feedback")
        require(type(state) is StateValue and not self._sequence_failed, "sequence state is unavailable or mistyped")
        before = self._value("proteus.eval.state@1", self._state)
        after = self._value("proteus.eval.state@1", state)
        update = StateUpdate(after_rollout_id=self._observations[-1].rollout.id, before=before, after=after)
        self._save(f"updates/{len(self._updates):06d}.json", update)
        self._updates.append(update)
        self._state = state

    def finish(self):
        return self.services._record("evaluation.finish", {"operation_id": self.operation_id, "position": self.position},
            self._finish_checked, decode=EvaluationReceipt.from_dict)

    def _finish_checked(self):
        self._check()
        require(len(self._observations) == len(self.batch.tasks), "cannot finish an incomplete task cohort")
        try:
            return self._finish()
        except RecoveryRequired:
            raise
        except BaseException as exc:
            if self.services._journal is not None and not isinstance(exc, Exception):
                raise
            self.abort("evaluation finalization did not commit; automatic replay is unavailable")
            raise

    def _finish(self):
        for index in range(len(self._observations)):
            if index not in self._grades:
                self._grade(index)
        cohorts = {}
        for index, observation in enumerate(self._observations):
            cohorts.setdefault(observation.rollout.candidate_id, []).append(index)
        batches = []
        for number, (candidate_id, indices) in enumerate(cohorts.items()):
            rollouts = [self._observations[index].rollout for index in indices]
            batch = EvaluationBatch(id=f"{self.operation_id}:batch-{number + 1}", run_id=self.services.run_id,
                operation_id=self.operation_id, attempt_id=self.request.attempt_id, candidate_id=candidate_id,
                artifact_hash=rollouts[0].artifact_hash, evaluator=self.spec.evaluator.plugin,
                protocol_hash=self.protocol_hash, expected_rollout_ids=tuple(rollout.id for rollout in rollouts),
                rows=tuple(self._grades[index] for index in indices), visibility=self.spec.visibility,
                usage=sum_usage(self._grade_usage[index] for index in indices))
            self._save(f"private/batches/{number:06d}.json", batch)
            if self.spec.visibility == FeedbackVisibility.OBSERVED:
                batches.append(batch)
        receipt = EvaluationReceipt(id=self.operation_id, task_batch_hash=content_hash(self.batch),
            rollout_ids=tuple(item.rollout.id for item in self._observations),
            candidate_ids=tuple(item.rollout.candidate_id for item in self._observations), batches=tuple(batches))
        self._save("receipt.json", receipt)
        for batch in batches:
            self._value("proteus.eval.batch@1", batch)
        ref = self._value("proteus.eval.receipt@1", receipt)
        self.services.ledger.settle(self.ledger_entry, actual=Amounts.zero(evaluations=len(self.batch.tasks)),
            status="completed", result={"receipt": ref})
        self.services.evaluations.extend(batches)
        self.services.results.append(StageResult(run_id=self.services.run_id, operation_id=self.operation_id,
            attempt_id=self.request.attempt_id, status=OperationStatus.COMPLETED, outputs={"evaluation": ref},
            usage=sum_usage([*(item.rollout.usage for item in self._observations),
                             *self._grade_usage.values(), *self._build_usage])))
        self.closed = True
        return receipt

    def abort(self, reason):
        require(threading.get_ident() == self.services._dispatch_thread, "evaluation requires serial service dispatch")
        if self.closed:
            return
        self.closed = True
        pending = [usage for usage in (self._pending_usage, self._pending_grade_usage) if usage is not None]
        pending.extend(usage for index, usage in self._backend_usage.items() if index not in self._grade_usage)
        self.services.results.append(StageResult(run_id=self.services.run_id, operation_id=self.operation_id,
            attempt_id=self.request.attempt_id, status=OperationStatus.FAILED,
            usage=sum_usage([*(item.rollout.usage for item in self._observations),
                             *self._grade_usage.values(), *self._build_usage, *pending]),
            diagnostic=reason))
        self._save("incomplete.json", {"reason": reason, "completed_predictions": len(self._observations),
                                       "expected_predictions": len(self.batch.tasks)})
        self.services.ledger.settle(self.ledger_entry, actual=Amounts.zero(evaluations=len(self.batch.tasks)),
            status="failed", result={"reason": reason, "completed_predictions": len(self._observations)})
