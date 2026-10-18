"""Contracts for bounded, replayable programs inside a method.

A workflow is trusted Python control flow over an explicit subset of stages and
formal evaluations. It is not a second experiment and never owns a separate
budget. Its external operations must use the run's managed services.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import threading
import time
from types import MappingProxyType

from proteus.core.contracts import (
    DataRef,
    FeedbackVisibility,
    NONNEGATIVE,
    OperationStatus,
    SessionPolicy,
    StatePolicy,
    TEXT,
    require,
)
from proteus.core.serialization import (
    Contract,
    ContractError,
    canonical_json,
    content_hash,
    read_json,
)
from proteus.core.methods import MethodResult, RunServices
from proteus.core.recovery import Journal, RecoveryRequired, _load, _write


@dataclass(frozen=True, kw_only=True)
class WorkflowJob(Contract):
    key: str = field(metadata=TEXT)
    workflow: str = field(metadata=TEXT)
    inputs: Mapping[str, DataRef]


@dataclass(frozen=True, kw_only=True)
class WorkflowResult(Contract):
    operation_id: str = field(metadata=TEXT)
    status: OperationStatus
    outputs: Mapping[str, DataRef] = field(default_factory=dict)
    diagnostic: str = ""

    def _validate(self):
        require(
            self.status in (OperationStatus.COMPLETED, OperationStatus.FAILED),
            "workflow results must describe a settled program",
        )
        require(
            self.status == OperationStatus.COMPLETED or not self.outputs,
            "failed workflows cannot publish successful outputs",
        )


@dataclass(frozen=True, kw_only=True)
class WorkflowMapResult(Contract):
    operation_id: str = field(metadata=TEXT)
    keys: tuple[str, ...]
    results: tuple[WorkflowResult, ...]
    cancelled: tuple[str, ...] = ()
    wall_seconds: float | None = field(default=None, metadata=NONNEGATIVE)
    resumed: bool = False

    def _validate(self):
        require(len(self.keys) == len(self.results), "workflow map result keys differ")
        require(
            len(set((*self.keys, *self.cancelled))) == len(self.keys) + len(self.cancelled),
            "workflow map keys must be unique",
        )
        require(
            not self.resumed or self.wall_seconds is None,
            "interrupted workflows cannot claim continuous elapsed time",
        )

    @property
    def succeeded(self):
        return not self.cancelled and all(
            row.status == OperationStatus.COMPLETED for row in self.results
        )


def validate_workflows(recipe, registry):
    stages = {stage.id: stage for stage in recipe.stages}
    for workflow in recipe.workflows:
        roles = set()
        for name in workflow.stages:
            stage = stages[name]
            capabilities = registry.get(stage.operator.plugin).capabilities
            require(
                {"stage.parallel", "stage.replay"} <= set(capabilities),
                f"{workflow.id}: stages require independent execution and replay",
            )
            roles.add(stage.role)
        if workflow.evaluations:
            require(
                recipe.subject is not None and recipe.execution is not None,
                "workflow evaluations require a subject and execution",
            )
            require(
                recipe.execution.state_policy == StatePolicy.INDEPENDENT,
                "workflow evaluations require independent task state",
            )
            require(
                "subject.parallel" in registry.get(recipe.subject.runner.plugin).capabilities,
                "workflow subject must support parallel execution",
            )
            require(
                "environment.parallel"
                in registry.get(recipe.execution.environment.backend.plugin).capabilities,
                "workflow environment must support parallel execution",
            )
            if recipe.subject.actor:
                roles.add(recipe.subject.actor)
        for role in roles:
            actor = recipe.actors[role]
            require(
                actor.session == SessionPolicy.FRESH
                and "actor.parallel" in registry.get(actor.runtime.plugin).capabilities,
                f"{workflow.id}: roles require independent fresh parallel actors",
            )


class _ReplayBoundary(RecoveryRequired):
    """An internal recovery barrier, never a failed program or new dispatch."""


class _Admission:
    def __init__(self, ledger):
        self.ledger = ledger
        self.condition = threading.Condition()
        self.stopped = False

    def stop(self):
        with self.condition:
            self.stopped = True
            self.condition.notify_all()


class _BranchLedger:
    """Wait for shared role capacity while preserving the ledger's exact caps."""

    def __init__(self, admission, replay_only):
        self.admission, self.replay_only = admission, replay_only

    def __getattr__(self, name):
        return getattr(self.admission.ledger, name)

    def reserve(self, entry_id, **kwargs):
        admission = self.admission
        with admission.condition:
            if self.replay_only:
                entry = next((e for e in self.entries() if e["id"] == entry_id), None)
                kind = kwargs["metadata"]["kind"]
                # These two entries do not themselves execute an external call.
                bookkeeping = kind in ("evaluation_cohort", "backend_verifier")
                if entry is None or (entry["state"] == "reserved" and not bookkeeping):
                    raise _ReplayBoundary("workflow reached undispatched work")
            while True:
                if admission.stopped:
                    raise RecoveryRequired("workflow map stopped with unresolved branch work")
                try:
                    return admission.ledger.reserve(entry_id, **kwargs)
                except ContractError as exc:
                    if not str(exc).startswith("concurrency budget exhausted:"):
                        raise
                    # No reservation is written on a failed capacity check. A
                    # role being busy is not an inference or evaluation failure.
                    admission.condition.wait(timeout=0.05)

    def settle(self, *args, **kwargs):
        with self.admission.condition:
            value = self.admission.ledger.settle(*args, **kwargs)
            self.admission.condition.notify_all()
            return value


class _Branch(RunServices):
    allowed_operations = {
        "candidate",
        "stage",
        "evaluation.pool",
        "evaluation.supervision",
        "evaluation.open",
        "evaluation.step",
        "evaluation.finish",
    }

    def _record(self, kind, identity, *args, **kwargs):
        require(kind in self.allowed_operations, f"workflow does not support nested {kind}")
        if "evaluation_id" in identity:
            require(
                identity["evaluation_id"] in self.workflow.evaluations,
                "workflow cannot access an undeclared evaluation",
            )
        if "operation_id" in identity:
            require(
                identity["operation_id"].startswith(self.namespace + ":"),
                "workflow operation escaped its namespace",
            )
        if (
            self.replay_only
            and not self._journal._path(self._journal.cursor + 1, "request").exists()
        ):
            raise _ReplayBoundary("workflow replay reached a new operation")
        return super()._record(kind, identity, *args, **kwargs)

    def name(self, value=None):
        return self.namespace + ":" + (value or f"op-{len(self._operations) + 1:06d}")

    def stage(self, stage_id, inputs, *, operation_id=None):
        require(stage_id in self.workflow.stages, "workflow cannot dispatch an undeclared stage")
        return super().stage(stage_id, inputs, operation_id=self.name(operation_id))

    def open_evaluation(self, evaluation_id, *, operation_id=None, selection=None, inputs=None):
        return super().open_evaluation(
            evaluation_id, operation_id=self.name(operation_id), selection=selection, inputs=inputs
        )

    def evaluate(self, candidate, evaluation_id, *, operation_id=None, selection=None, inputs=None):
        # The workflow pool owns concurrency. Each program's steps remain serial,
        # including when the owner's stand-alone evaluations use trial maps.
        session = self.open_evaluation(
            evaluation_id, operation_id=operation_id, selection=selection, inputs=inputs
        )
        for _ in session.batch.tasks:
            session.step(candidate)
        return session.finish()

    def _candidate(
        self, artifact, delta, parents, candidate_id, operation_id, validation, checkpoint
    ):
        return super()._candidate(
            artifact,
            delta,
            parents,
            self.name(candidate_id or f"candidate-{len(self.candidates) + 1:06d}"),
            self.name(operation_id),
            validation,
            checkpoint,
        )

    def checkpoint(self, *args, **kwargs):
        raise ValueError(
            "workflow programs replay managed operations; method checkpoints belong to the owner"
        )


def map_status(root, operation_id):
    directory = root / "private/workflows" / content_hash(operation_id).split(":")[1]
    if not (directory / "plan.json").exists():
        return []
    plan = _load(directory / "plan.json")
    rows = []
    for job in plan["jobs"]:
        branch = directory / WorkflowMap.child_id(operation_id, job["key"])
        if (branch / "result.json").exists():
            state = _load(branch / "result.json")["result"]["status"]
        else:
            state = "submitted" if (branch / "request.json").exists() else "not_submitted"
        rows.append({"key": job["key"], "workflow": job["workflow"], "state": state})
    return rows


class WorkflowMap:
    def __init__(self, services):
        self.services = services
        self.admission = _Admission(services.ledger)
        self.build_lock = threading.Lock()

    @staticmethod
    def child_id(operation_id, key):
        return "workflow-" + content_hash([operation_id, key]).split(":")[1]

    def run(self, jobs, *, operation_id, max_workers, failure_policy):
        services = self.services
        require(
            threading.get_ident() == services._dispatch_thread,
            "workflow map requires its method owner",
        )
        require(services._journal is not None, "workflows require a journal-enabled method")
        require(
            type(operation_id) is str and operation_id.strip(),
            "workflow map requires a named operation",
        )
        require(type(max_workers) is int and max_workers > 0, "invalid workflow concurrency")
        require(failure_policy in ("collect", "fail_fast"), "invalid workflow failure policy")
        require(
            all(session.closed for session in services._evaluation_service.sessions),
            "close owner evaluations before dispatching workflows",
        )
        jobs = tuple(jobs)
        require(
            jobs and all(type(job) is WorkflowJob for job in jobs),
            "workflow map requires typed jobs",
        )
        require(len({job.key for job in jobs}) == len(jobs), "workflow job keys must be unique")
        specs = {spec.id: spec for spec in services.recipe.workflows}
        for job in jobs:
            require(job.workflow in specs, "unknown workflow")
            spec = specs[job.workflow]
            require(set(job.inputs) == set(spec.input_types), "workflow input ports differ")
            for name, ref in job.inputs.items():
                require(ref.kind == spec.input_types[name], "workflow input kind differs")
                services.values.get(ref)
        identity = {
            "operation_id": operation_id,
            "jobs": jobs,
            "max_workers": max_workers,
            "failure_policy": failure_policy,
        }
        return services._record(
            "workflow.map",
            identity,
            lambda: self._run(jobs, specs, identity),
            decode=WorkflowMapResult.from_dict,
        )

    def _run(self, jobs, specs, identity):
        services, operation = self.services, identity["operation_id"]
        services._check_operation(operation)
        services._operations.add(operation)
        baseline = read_json(canonical_json(services.snapshot()))
        plan = read_json(
            canonical_json(
                {
                    "run_id": services.run_id,
                    "config_hash": services.recipe.config_hash,
                    "lock_hash": services.lock_hash,
                    "baseline_hash": content_hash(baseline),
                    **identity,
                }
            )
        )
        directory = services.root / "private/workflows" / content_hash(operation).split(":")[1]
        resumed = (directory / "plan.json").exists()
        if resumed:
            require(_load(directory / "plan.json") == plan, "workflow plan or baseline changed")
        else:
            _write(directory / "plan.json", plan)
        started = time.monotonic()
        children = {job.key: self.child_id(operation, job.key) for job in jobs}
        completed, submitted = {}, set()
        for job in jobs:
            path = directory / children[job.key]
            if (path / "request.json").exists():
                require(
                    _load(path / "request.json") == self.request(plan, job),
                    "workflow request changed",
                )
                submitted.add(job.key)
            require(
                not (path / "result.json").exists() or job.key in submitted,
                "orphan workflow result",
            )
            if (path / "result.json").exists():
                completed[job.key] = self.read_result(path, job, baseline, children[job.key])
        self.preflight(children.values())
        limit = min(identity["max_workers"], services.recipe.budget.max_concurrency)
        # Existing branch programs run behind a no-new-dispatch fence. This lets
        # reconcilable subjects settle before ANY branch advances to new work.
        phases = [
            (True, [job for job in jobs if job.key in submitted and job.key not in completed]),
            (False, list(jobs)),
        ]
        cancelled = []
        for replay_only, phase in phases:
            pending = [job for job in phase if job.key not in completed]
            if not replay_only:
                held = [
                    e["identity"]["metadata"].get("operation_id", "")
                    for e in services.ledger.entries()
                    if e["state"] == "reserved" and e["identity"]["slots"]
                ]
                pending.sort(
                    key=lambda job: not any(op.startswith(children[job.key] + ":") for op in held)
                )
            errors, active = [], {}
            failed = any(
                row["result"].status == OperationStatus.FAILED for row in completed.values()
            )
            with ThreadPoolExecutor(
                max_workers=limit, thread_name_prefix="proteus-workflow"
            ) as pool:
                while pending or active:
                    stop = errors or (failed and identity["failure_policy"] == "fail_fast")
                    while pending and len(active) < limit:
                        job = next(
                            (
                                job
                                for job in pending
                                if not errors and (not stop or replay_only or job.key in submitted)
                            ),
                            None,
                        )
                        if job is None:
                            break
                        pending.remove(job)
                        path = directory / children[job.key]
                        _write(path / "request.json", self.request(plan, job))
                        active[
                            pool.submit(
                                self.branch,
                                job,
                                specs[job.workflow],
                                path,
                                baseline,
                                children[job.key],
                                replay_only,
                            )
                        ] = job
                    if not active:
                        break
                    done, _ = wait(active, return_when=FIRST_COMPLETED)
                    for future in done:
                        job = active.pop(future)
                        try:
                            record = future.result()
                            if record is not None:
                                completed[job.key] = record
                                failed |= record["result"].status == OperationStatus.FAILED
                        except BaseException as exc:
                            errors.append(exc)
                            self.admission.stop()
            if errors:
                raise RecoveryRequired(
                    f"workflow did not commit: {type(errors[0]).__name__}: {errors[0]}"
                )
            if not replay_only:
                # A submitted but unfinished program is not a cancelled job.
                require(
                    all(job.key not in submitted for job in pending),
                    "fail-fast cannot discard an unfinished submitted workflow",
                )
                cancelled = [job.key for job in pending]
        self.join([completed[job.key] for job in jobs if job.key in completed], baseline)
        result = WorkflowMapResult(
            operation_id=operation,
            keys=tuple(job.key for job in jobs if job.key in completed),
            results=tuple(completed[job.key]["result"] for job in jobs if job.key in completed),
            cancelled=tuple(cancelled),
            resumed=resumed,
            wall_seconds=None if resumed else time.monotonic() - started,
        )
        path = directory / "join.json"
        if path.exists():
            saved = WorkflowMapResult.from_dict(_load(path))
            require(
                saved.keys == result.keys
                and saved.results == result.results
                and saved.cancelled == result.cancelled
                and saved.operation_id == operation,
                "workflow join differs",
            )
            result = saved
        else:
            _write(path, result)
        return result

    @staticmethod
    def request(plan, job):
        return {"plan_hash": content_hash(plan), "job": job.to_dict()}

    def preflight(self, namespaces):
        services = self.services
        namespaces = tuple(namespace + ":" for namespace in namespaces)
        subject = services.recipe.subject
        reconciles = (
            subject is not None
            and "subject.reconcile" in services.registry.get(subject.runner.plugin).capabilities
        )
        for entry in services.ledger.entries():
            metadata = entry["identity"]["metadata"]
            belongs = metadata.get("operation_id", "").startswith(namespaces)
            if entry["state"] in ("started", "reserved") and entry["identity"]["slots"]:
                require(belongs, "workflow map cannot run while another operation holds slots")
            if not belongs or entry["state"] != "started":
                continue
            kind = metadata["kind"]
            if kind in ("actor", "source_build", "grader") or (
                kind == "subject" and not reconciles
            ):
                raise RecoveryRequired(f"workflow has an unsettled {kind}: {entry['id']}")

    def branch(self, job, spec, path, baseline, namespace, replay_only):
        from proteus.evaluation.parallel import SerializedBuilds

        parent = self.services
        recovering = (path / "journal/origin.json").exists()
        branch = _Branch(
            run_id=parent.run_id,
            recipe=parent.recipe,
            registry=parent.registry,
            root=parent.root,
            prompts=parent.prompts,
            lock_hash=parent.lock_hash,
            resume=recovering,
        )
        branch.namespace, branch.workflow, branch.replay_only = namespace, spec, replay_only
        branch._worker_id = namespace
        branch._ledger = _BranchLedger(self.admission, replay_only)
        branch._builds = SerializedBuilds(branch, self.build_lock)
        branch.restore(read_json(canonical_json(baseline)))
        branch.rng.seed(content_hash([namespace, parent.recipe.seed]))
        journal = Journal(
            branch,
            manifest_hash=content_hash(_load(path / "request.json")),
            validate_state=lambda state: require(False, "unexpected workflow checkpoint"),
        )
        journal.root = path / "journal"
        branch._journal = journal
        _, final = journal.initialize()
        if final is None:
            try:
                program = parent.registry.create(spec.operator, "workflow")
                outputs = program.run(
                    branch,
                    MappingProxyType(
                        {name: branch.values.get(ref) for name, ref in job.inputs.items()}
                    ),
                )
                require(
                    isinstance(outputs, Mapping) and set(outputs) == set(spec.output_types),
                    "workflow output ports differ",
                )
                for name, value in outputs.items():
                    require(
                        type(value) is parent.registry.data_schema(spec.output_types[name]),
                        "workflow output type differs",
                    )
                require(
                    all(session.closed for session in branch._evaluation_service.sessions),
                    "workflow returned with an unfinished evaluation",
                )
                final = MethodResult(
                    outputs={
                        name: branch.values.publish(spec.output_types[name], value)
                        for name, value in outputs.items()
                    }
                )
            except _ReplayBoundary:
                return None
            except RecoveryRequired:
                raise
            except Exception as exc:
                for session in branch._evaluation_service.sessions:
                    if not session.closed:
                        session.abort("workflow failed before its evaluation completed")
                final = MethodResult(
                    status=OperationStatus.FAILED, diagnostic=f"{type(exc).__name__}: {exc}"
                )
            final = journal.finish(final)
        else:
            final = MethodResult.from_dict(final)
        result = WorkflowResult(
            operation_id=namespace,
            status=final.status,
            outputs=final.outputs,
            diagnostic=final.diagnostic,
        )
        _write(
            path / "result.json",
            {
                "request_hash": content_hash(_load(path / "request.json")),
                "result": result,
                "snapshot": branch.snapshot(),
            },
        )
        return self.read_result(path, job, baseline, namespace)

    def read_result(self, path, job, baseline, namespace):
        services = self.services
        record = _load(path / "result.json")
        require(
            record["request_hash"] == content_hash(_load(path / "request.json")),
            "workflow receipt differs from its request",
        )
        result = WorkflowResult.from_dict(record["result"])
        require(result.operation_id == namespace, "workflow receipt namespace differs")
        snapshots = sorted((path / "journal/operations").glob("*.result.json"))
        require(snapshots, "workflow receipt has no operation journal")
        last = snapshots[-1]
        terminal = _load(last)
        request = _load(last.with_name(last.name.replace(".result.json", ".request.json")))
        require(
            request["kind"] == "method.result"
            and terminal["error"] is None
            and terminal["request_hash"] == content_hash(request)
            and terminal["snapshot"] == record["snapshot"],
            "workflow terminal journal differs",
        )
        final = MethodResult.from_dict(terminal["output"])
        require(
            final.status == result.status
            and final.outputs == result.outputs
            and final.diagnostic == result.diagnostic,
            "workflow result differs from its journal",
        )
        snapshot = record["snapshot"]
        lists = (
            "values",
            "candidates",
            "rollouts",
            "evaluations",
            "requests",
            "results",
            "actor_calls",
            "sessions",
            "builds",
        )
        for name in lists:
            require(
                snapshot[name][: len(baseline[name])] == baseline[name],
                f"workflow changed its inherited {name}",
            )
        require(
            snapshot["native_prompts"] == baseline["native_prompts"],
            "workflow changed native state",
        )
        operations = set(snapshot["operations"]) - set(baseline["operations"])
        require(
            set(baseline["operations"]) <= set(snapshot["operations"])
            and all(op.startswith(namespace + ":") for op in operations),
            "workflow operation scope differs",
        )
        require(
            all(
                row["operation_id"] in operations
                for row in snapshot["requests"][len(baseline["requests"]) :]
            ),
            "workflow request operation differs",
        )
        require(
            all(
                row["id"].startswith(namespace + ":") and row["created_by"] in operations
                for row in snapshot["candidates"][len(baseline["candidates"]) :]
            ),
            "workflow candidate scope differs",
        )
        checked = RunServices(
            run_id=services.run_id,
            recipe=services.recipe,
            registry=services.registry,
            root=services.root,
            lock_hash=services.lock_hash,
            prompts=services.prompts,
        )
        checked.restore(snapshot)
        for ref in result.outputs.values():
            checked.values.get(ref)
        entries = {
            entry["id"]: entry
            for entry in services.ledger.entries()
            if entry["identity"]["metadata"].get("operation_id", "").startswith(namespace + ":")
        }
        require(
            all(entry["settlement"] is not None for entry in entries.values()),
            "workflow finished with unsettled dispatches",
        )
        calls = snapshot["actor_calls"][len(baseline["actor_calls"]) :]
        require(
            {call["id"] for call in calls}
            == {
                key
                for key, entry in entries.items()
                if entry["identity"]["metadata"]["kind"] == "actor"
            }
            and len({c["id"] for c in calls}) == len(calls),
            "workflow actor coverage differs",
        )
        for call in calls:
            require(
                entries[call["id"]]["settlement"]["result"] == call,
                "workflow actor differs from ledger",
            )
        ordinals = {tuple(key): value for key, value in baseline["actor_ordinals"]}
        for call in calls:
            metadata = entries[call["id"]]["identity"]["metadata"]
            key = (metadata["operation_id"], metadata["attempt_id"], metadata["role"])
            if metadata.get("trial_id") is not None:
                key += (metadata["trial_id"],)
            ordinals[key] = max(ordinals.get(key, 0), metadata["ordinal"])
        require(checked._actor_ordinals == ordinals, "workflow call ordinals differ from ledger")
        outcomes = {}
        for rollout in snapshot["rollouts"][len(baseline["rollouts"]) :]:
            subject = [
                entry
                for entry in entries.values()
                if entry["identity"]["metadata"].get("trial_id") == rollout["id"]
                and entry["identity"]["metadata"]["kind"] == "subject"
            ]
            require(len(subject) == 1, "workflow rollout has no unique subject receipt")
            outcome = subject[0]["settlement"]["result"]["subject_result"]
            outcomes[rollout["id"]] = outcome
            identity = subject[0]["identity"]["request"]
            require(
                outcome is not None
                and all(
                    outcome[key] == rollout[key] for key in ("output", "status", "runtime", "usage")
                )
                and outcome["loaded_artifact_hash"] == rollout["artifact_hash"],
                "workflow prediction differs from ledger",
            )
            require(
                rollout["candidate_id"] == identity["candidate"]["id"]
                and rollout["artifact_hash"] == identity["candidate"]["artifact"]["content_hash"]
                and rollout["task_id"] == identity["task"]["id"]
                and rollout["task_version"] == identity["task"]["version"]
                and rollout["split"] == identity["task"]["split"],
                "workflow trial binding differs",
            )
            trace = checked.values.get(DataRef.from_dict(rollout["trace"][0]))
            require(trace.to_dict() == outcome["trace"], "workflow trial trace differs")
        builds = snapshot["builds"][len(baseline["builds"]) :]
        require(
            {build["id"] for build in builds}
            == {
                key
                for key, entry in entries.items()
                if entry["identity"]["metadata"]["kind"] == "source_build"
            }
            and len({build["id"] for build in builds}) == len(builds),
            "workflow build coverage differs",
        )
        for build in builds:
            require(
                entries[build["id"]]["settlement"]["result"]["receipt"] == build,
                "workflow build differs from ledger",
            )
        seen_trials, published_batches = set(), []
        for session in checked._evaluation_service.sessions[len(baseline["sessions"]) :]:
            require(session.closed, "workflow left an open evaluation")
            require(
                session.operation_id in operations
                and session.spec.id in {spec.id for spec in services.recipe.evaluations},
                "workflow evaluation scope differs",
            )
            cohort = entries[session.ledger_entry]
            expected = read_json(
                canonical_json(
                    {
                        "evaluation": session.spec,
                        "batch": session.batch,
                        "execution": services.recipe.execution,
                        "inputs": session.input_refs,
                    }
                )
            )
            require(
                cohort["identity"]["request"] == expected
                and read_json((session.root / "protocol.json").read_text())
                == {**expected, "reserved_evaluations": len(session.batch.tasks)},
                "workflow evaluation protocol differs",
            )
            require(
                session.protocol_hash
                == content_hash(
                    {
                        "spec": session.spec,
                        "batch": session.batch,
                        "subject": services.recipe.subject,
                        "execution": services.recipe.execution,
                        "lock_hash": services.lock_hash,
                        "inputs": session.input_refs,
                    }
                ),
                "workflow evaluation protocol hash differs",
            )
            require(
                len(session._observations) == len(session._trials),
                "workflow observation coverage differs",
            )
            for index, trial in enumerate(session._trials):
                require(
                    trial.rollout.id not in seen_trials
                    and trial.rollout.to_dict() in snapshot["rollouts"][len(baseline["rollouts"]) :]
                    and session._observations[index].rollout == trial.rollout,
                    "workflow session prediction differs",
                )
                seen_trials.add(trial.rollout.id)
                require(
                    read_json((session.root / f"predictions/{index:06d}.json").read_text())
                    == trial.to_dict(),
                    "workflow prediction file differs",
                )
                outcome = outcomes[trial.rollout.id]
                require(
                    checked.values.get(trial.rollout.state_before) == session.batch.initial_state
                    and checked.values.get(trial.rollout.state_after).to_dict()
                    == (
                        outcome["state"]
                        if outcome["status"] in ("succeeded", "task_failed")
                        else session.batch.initial_state.to_dict()
                    ),
                    "workflow trial state differs",
                )
                backend = session._backend_evaluations.get(index)
                require(
                    (backend.to_dict() if backend else None) == outcome["backend_evaluation"],
                    "workflow backend evidence differs",
                )
                if session._backend():
                    verifier = [
                        entry
                        for entry in entries.values()
                        if entry["identity"]["metadata"].get("trial_id") == trial.rollout.id
                        and entry["identity"]["metadata"]["kind"] == "backend_verifier"
                    ]
                    subject = next(
                        entry
                        for entry in entries.values()
                        if entry["identity"]["metadata"].get("trial_id") == trial.rollout.id
                        and entry["identity"]["metadata"]["kind"] == "subject"
                    )
                    rejected = subject["settlement"]["result"]["rejected_result"]
                    expected_backend = (
                        rejected["backend_evaluation"]
                        if rejected
                        else outcome["backend_evaluation"]
                    )
                    require(
                        len(verifier) == 1
                        and verifier[0]["settlement"]["result"]["backend_evidence"]
                        == expected_backend,
                        "workflow verifier differs from ledger",
                    )
            for index, row in session._grades.items():
                grade = read_json((session.root / f"private/grades/{index:06d}.json").read_text())
                require(
                    grade["row"] == row.to_dict()
                    and grade["grade"]["usage"] == session._grade_usage[index].to_dict(),
                    "workflow grade file differs",
                )
                if not session._backend():
                    grader = [
                        entry
                        for entry in entries.values()
                        if entry["identity"]["metadata"].get("trial_id") == row.rollout_id
                        and entry["identity"]["metadata"]["kind"] == "grader"
                    ]
                    require(
                        len(grader) == 1
                        and grader[0]["settlement"]["result"]["grade"] == grade["grade"],
                        "workflow grade differs from ledger",
                    )
                require(
                    row.score == grade["grade"]["score"]
                    and row.status.value == grade["grade"]["status"]
                    and dict(row.metrics) == grade["grade"]["metrics"],
                    "workflow score differs from grade",
                )
                observation = session._observations[index]
                if observation.row is not None:
                    require(
                        session.spec.visibility == FeedbackVisibility.OBSERVED
                        and observation.row == row
                        and observation.feedback.to_dict() == grade["grade"]["feedback"],
                        "workflow released feedback differs",
                    )
            for path in sorted((session.root / "private/batches").glob("*.json")):
                batch = read_json(path.read_text())
                require(
                    batch["operation_id"] == session.operation_id
                    and batch["rows"]
                    == [
                        row.to_dict()
                        for _, row in sorted(session._grades.items())
                        if row.rollout_id in batch["expected_rollout_ids"]
                    ],
                    "workflow batch grades differ",
                )
                if session.spec.visibility == FeedbackVisibility.OBSERVED:
                    published_batches.append(batch)
        require(seen_trials == set(outcomes), "workflow session trial coverage differs")
        require(
            snapshot["evaluations"][len(baseline["evaluations"]) :] == published_batches,
            "workflow public evaluations differ from private grades",
        )
        return {"result": result, "snapshot": snapshot, "services": checked}

    def join(self, records, baseline):
        services = self.services
        session_snapshots = services._evaluation_service.snapshot()
        for record in records:
            snapshot, checked = record["snapshot"], record["services"]
            for ref, value in checked.values._values.values():
                require(services.values.publish(ref.kind, value) == ref, "workflow value differs")
            for name in (
                "candidates",
                "rollouts",
                "evaluations",
                "requests",
                "results",
                "actor_calls",
            ):
                getattr(services, name).extend(getattr(checked, name)[len(baseline[name]) :])
            services.builds.records.extend(checked.builds.records[len(baseline["builds"]) :])
            operations = set(snapshot["operations"]) - set(baseline["operations"])
            require(
                not operations.intersection(services._operations), "workflow operation collision"
            )
            services._operations.update(operations)
            inherited = {tuple(key): value for key, value in baseline["actor_ordinals"]}
            require(
                all(checked._actor_ordinals.get(key) == value for key, value in inherited.items()),
                "workflow changed inherited actor ordinals",
            )
            for key, value in checked._actor_ordinals.items():
                if key not in inherited:
                    require(key not in services._actor_ordinals, "workflow actor ordinal collision")
                    services._actor_ordinals[key] = value
            session_snapshots.extend(snapshot["sessions"][len(baseline["sessions"]) :])
        services._evaluation_service.restore(session_snapshots)
