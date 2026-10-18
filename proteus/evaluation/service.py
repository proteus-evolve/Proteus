"""Serial evaluation sessions with prediction-before-feedback publication.

Each session commits its complete declared task cohort. Methods may replace the
candidate between steps for online learning; per-candidate batches retain their
actual task membership. Hidden grades stay in private records and never enter the
public Values store or the subject's history. Plugins remain trusted Python code.
"""

from __future__ import annotations

import threading
import time
from dataclasses import replace

from proteus.artifacts.store import publish_bytes
from proteus.core.contracts import (
    Candidate, DataRef, EvaluationBatch, EvaluationRow, FeedbackVisibility, OperationStatus, Outcome,
    PluginRef, Rollout, RuntimeIdentity, StageRequest, StageResult, StatePolicy, Usage, ValidationStatus, require,
)
from proteus.core.serialization import canonical_json, content_hash
from proteus.evaluation.records import (
    EvaluationReceipt, Grade, Observation, StateUpdate, SubjectResult, TrialRecord,
    sum_usage, zero_usage,
)
from proteus.evaluation.runners import EnvironmentLease, InvalidCandidate, SubjectContext
from proteus.evaluation.tasks import StateValue, make_batch
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
        require("evaluator.deterministic" in registry.get(spec.evaluator.plugin, "evaluator").capabilities,
                "P03 evaluators must be deterministic; actor-backed grading needs a managed grader role")
    subject = recipe.subject
    subject_plugin = registry.get(subject.runner.plugin, "subject")
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
              position, seed, lock_hash, implementations):
    """An identity definition, not a cache hit/reuse policy. P03 always executes."""
    return content_hash({"artifact": candidate.artifact, "task": task, "dataset": batch.dataset_hash,
        "subject": subject, "actors": actors, "evaluation": evaluation, "execution": execution,
        "initial_state": batch.initial_state, "state": state, "history": history,
        "position": position, "seed": seed, "lock_hash": lock_hash, "implementations": implementations})


class EvaluationService:
    def __init__(self, services):
        self.services = services
        self.sessions = []
        self.reserved = 0

    def open(self, evaluation_id, *, operation_id=None):
        services = self.services
        require(threading.get_ident() == services._dispatch_thread, "evaluation requires serial service dispatch")
        require(not services.resume, "evaluation recovery requires the durable P04 operation ledger")
        validate_evaluation_config(services.recipe, services.registry)
        spec = next((spec for spec in services.recipe.evaluations if spec.id == evaluation_id), None)
        require(spec is not None, f"unknown declared evaluation: {evaluation_id}")
        dataset = services.registry.create(spec.tasks.suite, "task_suite").load()
        batch = make_batch(dataset, spec.tasks)
        execution = services.recipe.execution
        require(execution.initial_state_hash is None or execution.initial_state_hash == content_hash(batch.initial_state),
                "initial state differs from ExecutionSpec")
        limit = services.recipe.budget.max_evaluations
        require(limit is None or self.reserved + len(batch.tasks) <= limit, "evaluation budget exhausted")
        operation_id = operation_id or f"op-{len(services._operations) + 1:06d}"
        require(operation_id not in services._operations, "operation already submitted")
        session = EvaluationSession(self, spec, dataset, batch, operation_id)
        self.reserved += len(batch.tasks)
        services._operations.add(operation_id)
        self.sessions.append(session)
        return session

    def finalize_incomplete(self):
        for session in self.sessions:
            if not session.closed:
                session.abort("method returned before completing its declared evaluation cohort")


class EvaluationSession:
    def __init__(self, owner, spec, dataset, batch, operation_id):
        self.owner, self.services, self.spec, self.batch = owner, owner.services, spec, batch
        self.operation_id = operation_id
        self.closed, self._running, self._sequence_failed = False, False, False
        self._observations, self._grades, self._grade_usage, self._trials = [], {}, {}, []
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
            "execution": self.services.recipe.execution, "reserved_evaluations": len(batch.tasks)})
        self.request = StageRequest(run_id=self.services.run_id, operation_id=operation_id, attempt_id="attempt-1",
            stage=PluginRef(name="proteus.evaluation", version="1"), role="framework",
            inputs={"tasks": self._value("proteus.eval.task_batch@1", batch)},
            read_scopes=("proteus.candidate@1", "proteus.eval.task_batch@1"),
            write_scopes=("proteus.eval.receipt@1",), budget=self.services.recipe.budget)
        self.services.requests.append(self.request)
        self.protocol_hash = content_hash({"spec": spec, "batch": batch,
            "subject": self.services.recipe.subject, "execution": self.services.recipe.execution,
            "lock_hash": self.services.lock_hash})

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

    def step(self, candidate: Candidate) -> Observation:
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
        except BaseException:
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
        self._pending_usage = Usage()
        started = time.monotonic()
        runtime_ref = recipe.actors[recipe.subject.actor].runtime.plugin if recipe.subject.actor else recipe.subject.runner.plugin
        runtime = RuntimeIdentity(runtime=runtime_ref, environment_hash=content_hash(recipe.execution.environment))
        first_actor = len(services.actor_calls)
        try:
            if self._sequence_failed:
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
                with environment.open(candidate.artifact, services.artifacts,
                        self.root / "trials" / f"{index:06d}", recipe.execution) as lease:
                    require(isinstance(lease, EnvironmentLease)
                            and lease.loaded_artifact_hash == candidate.artifact.content_hash,
                            "environment loaded a different candidate")
                    actor = None
                    if recipe.subject.actor is not None:
                        stage = StageSpec(id="subject", operator=recipe.subject.runner, role=recipe.subject.actor)
                        def actor(prompt):
                            return services._actor(stage, self.request, prompt, None, None, isolate=True)
                    context = SubjectContext(candidate=candidate, task=task, state=state, history=history,
                        seed=seed, execution=recipe.execution, subject=recipe.subject, lease=lease, _actor=actor)
                    # New runner instance per task prevents undeclared plugin-instance memory.
                    result = services.registry.create(recipe.subject.runner, "subject").run(context)
                require(type(result) is SubjectResult, "subject must return SubjectResult")
                require(result.loaded_artifact_hash == candidate.artifact.content_hash,
                        "subject reported a different loaded candidate")
                require(result.runtime.runtime == runtime_ref
                        and result.runtime.environment_hash == runtime.environment_hash,
                        "subject runtime/environment differs from configured execution")
                if recipe.subject.actor is not None:
                    usage = services._usage(first_actor)
                    require(result.usage.model_calls == usage.model_calls, "subject actor usage mismatch")
                    result = replace(result, usage=usage)
                elif "subject.model_free" in services.registry.get(recipe.subject.runner.plugin).capabilities:
                    require(result.usage.model_calls == 0, "model-free subject reported nonzero/unknown model calls")
        except Exception as exc:
            usage = services._usage(first_actor) if len(services.actor_calls) > first_actor else Usage()
            result = SubjectResult(loaded_artifact_hash=candidate.artifact.content_hash, runtime=runtime,
                status=Outcome.INVALID_CANDIDATE if isinstance(exc, InvalidCandidate) else Outcome.INFRA_ERROR,
                state=state, usage=usage,
                diagnostic=f"{type(exc).__name__}: {exc}")
        after = result.state if result.status in VALID else state
        usage = replace(result.usage, wall_seconds=time.monotonic() - started)
        self._pending_usage = usage
        rollout = Rollout(id=f"{self.operation_id}:rollout-{index + 1}", run_id=services.run_id,
            operation_id=self.operation_id, attempt_id=self.request.attempt_id,
            candidate_id=candidate.id, artifact_hash=candidate.artifact.content_hash,
            task_id=task.id, task_version=task.version, split=task.split,
            execution_hash=content_hash(recipe.execution), runtime=result.runtime, status=result.status,
            usage=usage, output=result.output,
            trace=(self._value("proteus.eval.trace@1", result.trace),),
            sequence_id=self.operation_id if sequence else None, step=index if sequence else None,
            state_before=state_ref, state_after=self._value("proteus.eval.state@1", after),
            diagnostic=result.diagnostic)
        implementations = {ref.name + "@" + ref.version: services.registry.get(ref).implementation_hash
                           for ref in recipe.plugin_refs()}
        key = cache_key(candidate=candidate, task=task, batch=self.batch, subject=recipe.subject,
            actors=recipe.actors, evaluation=self.spec, execution=recipe.execution, state=state,
            history=history, position=index, seed=seed, lock_hash=services.lock_hash, implementations=implementations)
        trial = TrialRecord(rollout=rollout, cache_key=key, task_hash=content_hash(task),
                            history_hash=content_hash(history), task_seed=seed)
        self._save(f"predictions/{index:06d}.json", trial)
        if sequence:
            self._state = after
            self._sequence_failed = self._sequence_failed or result.status not in VALID
        self._trials.append(trial)
        services.rollouts.append(rollout)
        self._pending_usage = None
        return Observation(rollout=rollout)

    def _grade(self, index):
        observation = self._observations[index]
        rollout = observation.rollout
        if index in self._grades:
            return observation
        started = time.monotonic()
        self._pending_grade_usage = Usage()
        if rollout.status not in VALID:
            grade = Grade(status=rollout.status, score=None, usage=zero_usage(), diagnostic=rollout.diagnostic)
        else:
            try:
                grader = self.services.registry.create(self.spec.evaluator, "evaluator")
                grade = grader.grade(self.batch.tasks[index], rollout, self._references[rollout.task_id])
                require(type(grade) is Grade and grade.usage.model_calls == 0,
                        "deterministic evaluator must return Grade with zero model calls")
            except (Exception, SystemExit) as exc:
                grade = Grade(status=Outcome.INFRA_ERROR, score=None, usage=Usage(),
                              diagnostic=f"{type(exc).__name__}: {exc}")
        grade = replace(grade, usage=replace(grade.usage, wall_seconds=time.monotonic() - started))
        self._pending_grade_usage = grade.usage
        hidden = self.spec.visibility == FeedbackVisibility.HIDDEN
        feedback = self._value("proteus.eval.feedback@1", grade.feedback, private=hidden)
        row = EvaluationRow(rollout_id=rollout.id, task_id=rollout.task_id, status=grade.status,
                            score=grade.score, metrics=grade.metrics, feedback=(feedback,), diagnostic=grade.diagnostic)
        self._save(f"private/grades/{index:06d}.json", {"row": row, "grade": grade})
        self._grades[index], self._grade_usage[index] = row, grade.usage
        self._pending_grade_usage = None
        return observation if hidden else Observation(rollout=rollout, row=row, feedback=grade.feedback)

    def update_state(self, state: StateValue):
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
        self._check()
        require(len(self._observations) == len(self.batch.tasks), "cannot finish an incomplete task cohort")
        try:
            return self._finish()
        except BaseException:
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
        self.services.evaluations.extend(batches)
        ref = self._value("proteus.eval.receipt@1", receipt)
        self.services.results.append(StageResult(run_id=self.services.run_id, operation_id=self.operation_id,
            attempt_id=self.request.attempt_id, status=OperationStatus.COMPLETED, outputs={"evaluation": ref},
            usage=sum_usage([*(item.rollout.usage for item in self._observations), *self._grade_usage.values()])))
        self.closed = True
        return receipt

    def abort(self, reason):
        require(threading.get_ident() == self.services._dispatch_thread, "evaluation requires serial service dispatch")
        if self.closed:
            return
        self.closed = True
        pending = [usage for usage in (self._pending_usage, self._pending_grade_usage) if usage is not None]
        self.services.results.append(StageResult(run_id=self.services.run_id, operation_id=self.operation_id,
            attempt_id=self.request.attempt_id, status=OperationStatus.FAILED,
            usage=sum_usage([*(item.rollout.usage for item in self._observations), *self._grade_usage.values(), *pending]),
            diagnostic=reason))
        self._save("incomplete.json", {"reason": reason, "completed_predictions": len(self._observations),
                                       "expected_predictions": len(self.batch.tasks)})
