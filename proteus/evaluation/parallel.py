"""One formal cohort, independent trial execution and an ordered durable join."""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from types import SimpleNamespace
import threading
import time

from proteus.core.contracts import StatePolicy, Usage, require
from proteus.core.methods import RunServices, Values
from proteus.core.recovery import RecoveryRequired, _load, _write
from proteus.core.serialization import Contract, canonical_json, content_hash, read_json
from proteus.evaluation.records import BackendEvaluation, Observation, SubjectResult, TrialRecord
from proteus.execution.builds import BuildReceipt, BuildService


@dataclass(frozen=True, kw_only=True)
class EvaluationMap(Contract):
    operation_id: str
    rollout_ids: tuple[str, ...]
    max_workers: int = field(metadata={"minimum": 1})
    resumed: bool = False
    wall_seconds: float | None = field(default=None, metadata={"minimum": 0})

    def _validate(self):
        require(len(set(self.rollout_ids)) == len(self.rollout_ids), "duplicate mapped trial")
        require(
            not self.resumed or self.wall_seconds is None,
            "resumed evaluation cannot claim an uninterrupted wall-clock measurement",
        )


class TrialServices(RunServices):
    def _record(self, *args, **kwargs):
        raise ValueError("parallel subjects cannot dispatch nested method operations")


class SerializedBuilds(BuildService):
    def __init__(self, services, lock):
        super().__init__(services)
        self.lock = lock

    def execute(self, *args, **kwargs):
        # BuildService's process lease is nonblocking. Serialize the complete
        # build/cache receipt transaction; each trial still owns its receipt.
        with self.lock:
            return super().execute(*args, **kwargs)


def map_status(root, operation_id):
    directory = root / "private/evaluation-maps" / content_hash(operation_id).split(":")[1]
    if not (directory / "plan.json").exists():
        return []
    plan = _load(directory / "plan.json")
    result = []
    for index, task_id in enumerate(plan["task_ids"]):
        branch = directory / f"{index:06d}"
        state = (
            "completed"
            if (branch / "result.json").exists()
            else ("submitted" if (branch / "request.json").exists() else "not_submitted")
        )
        result.append({"position": index, "task_id": task_id, "state": state})
    return result


class TrialMap:
    def __init__(self, session):
        self.session, self.services = session, session.services
        self.build_lock = threading.Lock()

    def run(self, candidate, max_workers):
        session, services = self.session, self.services
        session._check()
        require(
            services._journal is not None, "parallel evaluation requires a journal-enabled method"
        )
        require(
            services.recipe.execution.state_policy == StatePolicy.INDEPENDENT,
            "parallel evaluation requires independent tasks",
        )
        require(session.position == 0, "parallel evaluation requires a fresh complete cohort")
        require(
            candidate in services.candidates and candidate.run_id == services.run_id,
            "parallel evaluation requires a run candidate",
        )
        directory = (
            services.root
            / "private/evaluation-maps"
            / content_hash(session.operation_id).split(":")[1]
        )
        plan = {
            "run_id": services.run_id,
            "config_hash": services.recipe.config_hash,
            "lock_hash": services.lock_hash,
            "operation_id": session.operation_id,
            "candidate": candidate,
            "batch_hash": content_hash(session.batch),
            "task_ids": [task.id for task in session.batch.tasks],
            "max_workers": max_workers,
            "inputs": session.input_refs,
        }
        plan = read_json(canonical_json(plan))
        resumed = (directory / "plan.json").exists()
        if resumed:
            require(_load(directory / "plan.json") == plan, "parallel evaluation plan changed")
        else:
            _write(directory / "plan.json", plan)
        started = time.monotonic()
        positions = list(range(len(session.batch.tasks)))
        completed, submitted = {}, set()
        for index in positions:
            child = directory / f"{index:06d}"
            request = self.request(plan, index)
            if (child / "request.json").exists():
                require(_load(child / "request.json") == request, "parallel trial request changed")
                submitted.add(index)
            require(
                not (child / "result.json").exists() or index in submitted,
                "orphan parallel trial result",
            )
            if (child / "result.json").exists():
                completed[index] = self.read_result(child, candidate, index)
        trial_entries = {}
        can_reconcile = (
            "subject.reconcile"
            in services.registry.get(services.recipe.subject.runner.plugin).capabilities
        )
        for entry in services.ledger.entries():
            metadata = entry["identity"]["metadata"]
            if entry["state"] in ("started", "reserved") and entry["identity"]["slots"]:
                require(
                    metadata.get("operation_id") == session.operation_id,
                    "parallel evaluation cannot dispatch with another unresolved operation",
                )
            if metadata.get("operation_id") != session.operation_id:
                continue
            if metadata["kind"] == "subject":
                trial_entries[metadata["trial_id"]] = entry
            if entry["state"] == "started" and (
                metadata["kind"] in ("actor", "source_build")
                or (metadata["kind"] == "subject" and not can_reconcile)
            ):
                raise RecoveryRequired(
                    f"parallel evaluation has an unsettled {metadata['kind']} {entry['id']}"
                )
        recover_first = {
            index
            for index in submitted
            if index not in completed
            and (entry := trial_entries.get(f"{session.operation_id}:rollout-{index + 1}"))
            is not None
            and entry["state"] != "reserved"
        }
        limit = min(max_workers, services.recipe.budget.max_concurrency)
        if services.recipe.subject.actor:
            limit = min(
                limit, services.recipe.actors[services.recipe.subject.actor].budget.max_concurrency
            )
        if session.spec.actor and session.spec.feedback_timing == "after_prediction":
            # Each worker predicts then grades. Bound the whole trial by both
            # roles so admission never relies on a ledger rejection mid-trial.
            limit = min(limit, services.recipe.actors[session.spec.actor].budget.max_concurrency)
        # Finish recovery as a separate phase before issuing ANY new trial. A
        # pending Harbor worker can reconcile; an uncertain actor stops the map.
        for phase in (
            [index for index in positions if index in recover_first],
            [index for index in positions if index not in completed and index not in recover_first],
        ):
            errors, active = [], {}
            pending = list(phase)
            with ThreadPoolExecutor(max_workers=limit, thread_name_prefix="proteus-trial") as pool:
                while pending or active:
                    while pending and len(active) < limit and not errors:
                        index = pending.pop(0)
                        child = directory / f"{index:06d}"
                        _write(child / "request.json", self.request(plan, index))
                        active[
                            pool.submit(self.branch, candidate, index, child, index in submitted)
                        ] = index
                    if not active:
                        break
                    done, _ = wait(active, return_when=FIRST_COMPLETED)
                    for future in done:
                        index = active.pop(future)
                        try:
                            completed[index] = future.result()
                        except BaseException as exc:
                            errors.append(exc)
            if errors:
                raise RecoveryRequired(
                    f"parallel trial did not commit: {type(errors[0]).__name__}: {errors[0]}"
                )
        require(set(completed) == set(positions), "parallel cohort is incomplete")
        self.join(completed)
        result = EvaluationMap(
            operation_id=session.operation_id,
            rollout_ids=tuple(item.rollout.id for item in session._observations),
            max_workers=max_workers,
            resumed=resumed,
            wall_seconds=None if resumed else time.monotonic() - started,
        )
        path = directory / "join.json"
        if path.exists():
            saved = EvaluationMap.from_dict(_load(path))
            require(
                saved.operation_id == result.operation_id
                and saved.rollout_ids == result.rollout_ids
                and saved.max_workers == result.max_workers,
                "parallel evaluation join differs",
            )
            result = saved
        else:
            _write(path, result)
        services.values.publish("proteus.eval.map@1", result)
        return result

    @staticmethod
    def request(plan, index):
        return {
            "plan_hash": content_hash(plan),
            "position": index,
            "task_id": plan["task_ids"][index],
        }

    def branch(self, candidate, index, directory, recovering):
        from proteus.evaluation.service import EvaluationSession

        parent, original = self.services, self.session
        branch = TrialServices(
            run_id=parent.run_id,
            recipe=parent.recipe,
            registry=parent.registry,
            root=parent.root,
            prompts=parent.prompts,
            resume=recovering,
            lock_hash=parent.lock_hash,
        )
        branch._ledger = parent.ledger
        branch._journal = SimpleNamespace(recovering=recovering)
        branch._worker_id = f"{original.operation_id}:rollout-{index + 1}"
        branch._builds = SerializedBuilds(branch, self.build_lock)
        branch.candidates = [candidate]
        for name, ref in original.input_refs.items():
            require(
                branch.values.publish(ref.kind, parent.values.get(ref)) == ref,
                "trial input differs",
            )
        session = object.__new__(EvaluationSession)
        session.restore(branch._evaluation_service, read_json(canonical_json(original.snapshot())))
        observation = session._predict(candidate, index)
        if session.spec.feedback_timing == "after_prediction":
            observation = session._grade(index, observation)
        require(len(session._trials) == 1, "parallel trial produced an invalid prediction count")
        record = {
            "request_hash": content_hash(_load(directory / "request.json")),
            "observation": observation,
            "trial": session._trials[0],
            "values": branch.values.export(),
            "actor_calls": branch.actor_calls,
            "actor_ordinals": list(branch._actor_ordinals.items()),
            "builds": branch.builds.records,
            "build_usage": session._build_usage,
            "backend": session._backend_evaluations.get(index),
            "backend_usage": session._backend_usage.get(index),
            "grade_usage": session._grade_usage.get(index),
        }
        _write(directory / "result.json", record)
        return self.read_result(directory, candidate, index)

    def read_result(self, directory, candidate, index):
        session, services = self.session, self.services
        record = _load(directory / "result.json")
        require(
            record["request_hash"] == content_hash(_load(directory / "request.json")),
            "parallel trial receipt differs",
        )
        observation, trial = (
            Observation.from_dict(record["observation"]),
            TrialRecord.from_dict(record["trial"]),
        )
        rollout, task = observation.rollout, session.batch.tasks[index]
        require(
            (observation.row is not None) == (session.spec.feedback_timing == "after_prediction"),
            "parallel prediction feedback timing differs",
        )
        require(
            trial.rollout == rollout
            and rollout.id == f"{session.operation_id}:rollout-{index + 1}"
            and rollout.run_id == services.run_id
            and rollout.operation_id == session.operation_id
            and rollout.candidate_id == candidate.id
            and rollout.artifact_hash == candidate.artifact.content_hash
            and rollout.task_id == task.id
            and rollout.task_version == task.version
            and rollout.split == task.split
            and trial.task_hash == content_hash(task)
            and trial.inputs == session.input_refs,
            "parallel trial binding differs",
        )
        require(
            trial.task_seed
            == int(
                content_hash(
                    [services.recipe.execution.seed, session.batch.seed, index, task.id]
                ).split(":")[1][:15],
                16,
            ),
            "parallel trial seed differs",
        )
        require(
            TrialRecord.from_json((session.root / f"predictions/{index:06d}.json").read_text())
            == trial,
            "parallel trial prediction file differs",
        )
        values = Values(services.registry)
        values.restore(record["values"])
        for ref in (
            *session.input_refs.values(),
            rollout.state_before,
            rollout.state_after,
            *rollout.trace,
        ):
            values.get(ref)
        entries = {
            entry["id"]: entry
            for entry in services.ledger.entries()
            if entry["identity"]["metadata"].get("trial_id") == rollout.id
        }
        require(
            all(entry["settlement"] is not None for entry in entries.values()),
            "parallel trial has unsettled usage",
        )
        subjects = [
            entry
            for entry in entries.values()
            if entry["identity"]["metadata"]["kind"] == "subject"
        ]
        require(len(subjects) == 1, "parallel trial subject receipt coverage differs")
        outcome = SubjectResult.from_dict(subjects[0]["settlement"]["result"]["subject_result"])
        require(
            outcome.output == rollout.output
            and outcome.status == rollout.status
            and outcome.runtime == rollout.runtime
            and outcome.usage == rollout.usage
            and outcome.loaded_artifact_hash == rollout.artifact_hash,
            "parallel trial differs from its settled subject result",
        )
        require(values.get(rollout.trace[0]) == outcome.trace, "parallel trial trace differs")
        require(
            values.get(rollout.state_before) == session.batch.initial_state
            and values.get(rollout.state_after)
            == (
                outcome.state
                if outcome.status.value in ("succeeded", "task_failed")
                else session.batch.initial_state
            ),
            "parallel trial state differs",
        )
        from proteus.evaluation.service import cache_key

        implementations = {
            ref.name + "@" + ref.version: services.registry.get(ref).implementation_hash
            for ref in services.recipe.plugin_refs()
        }
        require(
            trial.history_hash == content_hash(())
            and trial.cache_key
            == cache_key(
                candidate=candidate,
                task=task,
                batch=session.batch,
                subject=services.recipe.subject,
                actors=services.recipe.actors,
                evaluation=session.spec,
                execution=services.recipe.execution,
                state=session.batch.initial_state,
                history=(),
                position=index,
                seed=trial.task_seed,
                lock_hash=services.lock_hash,
                implementations=implementations,
                inputs=session.input_refs,
            ),
            "parallel trial cache identity differs",
        )
        require(
            (record["backend"] is None and outcome.backend_evaluation is None)
            or (
                outcome.backend_evaluation is not None
                and record["backend"] == outcome.backend_evaluation.to_dict()
            ),
            "parallel backend evidence differs",
        )
        graders = [
            entry for entry in entries.values() if entry["identity"]["metadata"]["kind"] == "grader"
        ]
        if session.spec.feedback_timing == "after_prediction":
            from proteus.evaluation.records import Grade

            grade_file = read_json((session.root / f"private/grades/{index:06d}.json").read_text())
            grade = Grade.from_dict(grade_file["grade"])
            require(
                grade_file["row"] == observation.row.to_dict()
                and grade.feedback == observation.feedback
                and grade.usage.to_dict() == record["grade_usage"],
                "parallel grade receipt differs",
            )
            if not session._backend():
                require(
                    len(graders) == 1
                    and graders[0]["settlement"]["result"]["grade"] == grade.to_dict(),
                    "parallel grade differs from ledger",
                )
            if session.spec.actor is not None:
                from proteus.evaluation.graders import grading_binding, verify_grade

                try:
                    verify_grade(
                        session.root / "private" / "grader-actors" / f"{index:06d}",
                        grade,
                        services.recipe.actors[session.spec.actor],
                        role=session.spec.actor,
                        binding=grading_binding(session.spec.id, rollout),
                    )
                except Exception as exc:
                    # Keep the cohort recoverable while its original evidence is
                    # reconciled, just as the serial grading path does.
                    raise RecoveryRequired(
                        "private grading evidence must be reconciled before parallel join"
                    ) from exc
        else:
            require(
                not graders and record["grade_usage"] is None,
                "parallel prediction graded before release",
            )
        actors = {
            key
            for key, entry in entries.items()
            if entry["identity"]["metadata"]["kind"] == "actor"
        }
        require(
            actors == {call["id"] for call in record["actor_calls"]}
            and len(actors) == len(record["actor_calls"]),
            "parallel actor receipt coverage differs",
        )
        for call in record["actor_calls"]:
            require(
                entries[call["id"]]["settlement"]["result"] == call,
                "parallel actor receipt differs from ledger",
            )
        expected_ordinals = {}
        for call in record["actor_calls"]:
            metadata = entries[call["id"]]["identity"]["metadata"]
            key = (
                metadata["operation_id"],
                metadata["attempt_id"],
                metadata["role"],
                metadata["trial_id"],
            )
            expected_ordinals[key] = max(expected_ordinals.get(key, 0), metadata["ordinal"])
        require(
            {tuple(key): ordinal for key, ordinal in record["actor_ordinals"]} == expected_ordinals,
            "parallel actor ordinals differ from settled calls",
        )
        builds = [BuildReceipt.from_dict(item) for item in record["builds"]]
        require(
            {item.id for item in builds}
            == {
                key
                for key, entry in entries.items()
                if entry["identity"]["metadata"]["kind"] == "source_build"
            },
            "parallel build receipt coverage differs",
        )
        for build in builds:
            require(
                entries[build.id]["settlement"]["result"]["receipt"] == build.to_dict(),
                "parallel build receipt differs",
            )
        return {
            **record,
            "typed_values": values,
            "observation": observation,
            "trial": trial,
            "builds": builds,
        }

    def join(self, records):
        session, services = self.session, self.services
        for index in range(len(session.batch.tasks)):
            record = records[index]
            for ref, value in record["typed_values"]._values.values():
                require(
                    services.values.publish(ref.kind, value) == ref, "parallel trial value differs"
                )
            session._observations.append(record["observation"])
            session._trials.append(record["trial"])
            services.rollouts.append(record["observation"].rollout)
            services.actor_calls.extend(record["actor_calls"])
            for key, ordinal in record["actor_ordinals"]:
                key = tuple(key)
                require(
                    len(key) == 4
                    and key[0] == session.operation_id
                    and key[3] == record["observation"].rollout.id,
                    "parallel actor ordinal namespace differs",
                )
                require(key not in services._actor_ordinals, "parallel actor was already joined")
                services._actor_ordinals[key] = ordinal
            services.builds.records.extend(record["builds"])
            session._build_usage.extend(Usage.from_dict(item) for item in record["build_usage"])
            if record["observation"].row is not None:
                session._grades[index] = record["observation"].row
                session._grade_usage[index] = Usage.from_dict(record["grade_usage"])
            if record["backend"] is not None:
                session._backend_evaluations[index] = BackendEvaluation.from_dict(record["backend"])
            if record["backend_usage"] is not None:
                session._backend_usage[index] = Usage.from_dict(record["backend_usage"])
