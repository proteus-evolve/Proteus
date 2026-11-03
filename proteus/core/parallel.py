"""Bounded stage maps with independent receipts and a single ordered join.

Only stage/actor code runs on worker threads. The method, shared Values and
operation journal stay on their owner thread. Each branch has its own runtime
instances and typed inputs; the common SQLite ledger arbitrates actual spend.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
import threading
import time
from types import SimpleNamespace

from proteus.core.contracts import (
    NONNEGATIVE,
    TEXT,
    DataRef,
    OperationStatus,
    StageRequest,
    StageResult,
    require,
)
from proteus.core.methods import RunServices, StageFailed
from proteus.core.recovery import RecoveryRequired, _load, _write
from proteus.core.serialization import Contract, canonical_json, content_hash, read_json


@dataclass(frozen=True, kw_only=True)
class StageJob(Contract):
    key: str = field(metadata=TEXT)
    stage: str = field(metadata=TEXT)
    inputs: Mapping[str, DataRef]


@dataclass(frozen=True, kw_only=True)
class StageMapResult(Contract):
    operation_id: str = field(metadata=TEXT)
    keys: tuple[str, ...]
    results: tuple[StageResult, ...]
    cancelled: tuple[str, ...] = ()
    wall_seconds: float | None = field(default=None, metadata=NONNEGATIVE)
    resumed: bool = False

    def _validate(self):
        require(len(self.keys) == len(self.results), "map result keys are not aligned")
        require(
            len(set((*self.keys, *self.cancelled))) == len(self.keys) + len(self.cancelled),
            "duplicate map result keys",
        )
        require(
            not self.resumed or self.wall_seconds is None,
            "interrupted maps cannot claim an uninterrupted wall-clock measurement",
        )

    @property
    def succeeded(self):
        return not self.cancelled and all(
            r.status == OperationStatus.COMPLETED for r in self.results
        )


class _Branch(RunServices):
    """A stage-local service instance, never a second method/journal writer."""

    def _record(self, *args, **kwargs):
        raise ValueError(
            "parallel stages may use only their StageContext; nested service dispatch is unsupported"
        )


def map_status(root, operation_id):
    """Inspect branch progress without disclosing inputs, outputs or prompts."""
    from pathlib import Path

    directory = Path(root) / "private/maps" / content_hash(operation_id).split(":")[1]
    if not (directory / "plan.json").exists():
        return []
    plan = _load(directory / "plan.json")
    require(plan["operation_id"] == operation_id, "map status operation differs")
    joined = _load(directory / "join.json") if (directory / "join.json").exists() else None
    branches = []
    for item in plan["jobs"]:
        operation = StageMap.child_id(operation_id, item["key"])
        child = directory / operation
        state = "not_submitted"
        if (child / "request.json").exists():
            state = "submitted"
        if (child / "result.json").exists():
            record = _load(child / "result.json")
            require(
                record["request_hash"] == content_hash(_load(child / "request.json")),
                "map status receipt differs",
            )
            state = record["result"]["status"]
        if joined and item["key"] in joined["cancelled"]:
            require(state == "not_submitted", "cancelled map branch was submitted")
            state = "cancelled"
        branches.append(
            {"key": item["key"], "stage": item["stage"], "operation_id": operation, "state": state}
        )
    return branches


class StageMap:
    def __init__(self, services):
        self.services = services

    def run(self, jobs, *, operation_id, max_workers, failure_policy):
        services = self.services
        require(
            threading.get_ident() == services._dispatch_thread,
            "cross-thread map dispatch requires the method owner",
        )
        require(services._journal is not None, "stage maps require a journal-enabled method")
        require(type(operation_id) is str and operation_id.strip(), "map needs a named operation")
        require(type(max_workers) is int and max_workers > 0, "invalid map concurrency")
        require(failure_policy in ("collect", "fail_fast"), "unknown map failure policy")
        jobs = tuple(jobs)
        require(
            bool(jobs) and all(type(job) is StageJob for job in jobs), "map needs typed stage jobs"
        )
        require(len({job.key for job in jobs}) == len(jobs), "map job keys must be unique")
        specs = {stage.id: stage for stage in services.recipe.stages}
        for job in jobs:
            require(job.stage in specs, "unknown map stage")
            stage = specs[job.stage]
            require(
                {"stage.parallel", "stage.replay"}
                <= set(services.registry.get(stage.operator.plugin).capabilities),
                "map stages must declare stage.parallel and stage.replay",
            )
            actor = services.recipe.actors[stage.role]
            require(
                actor.session.value == "fresh"
                and "actor.parallel" in services.registry.get(actor.runtime.plugin).capabilities,
                "map actors require independent fresh parallel runtimes",
            )
            require(set(job.inputs) == set(stage.input_types), "map input ports differ")
            for name, ref in job.inputs.items():
                require(ref.kind == stage.input_types[name], "map input kind differs")
                value = services.values.get(ref)
                if ref.kind == "proteus.evidence.view@1":
                    from proteus.evidence.service import EvidenceService

                    EvidenceService(services).bound(value, stage.id, verify_files=True)
        identity = {
            "operation_id": operation_id,
            "jobs": jobs,
            "max_workers": max_workers,
            "failure_policy": failure_policy,
        }
        return services._record(
            "stage.map",
            identity,
            lambda: self._run(jobs, specs, identity),
            decode=StageMapResult.from_dict,
        )

    @staticmethod
    def child_id(operation, key):
        return "map-" + content_hash([operation, key]).split(":")[1]

    def _run(self, jobs, specs, identity):
        services = self.services
        operation = identity["operation_id"]
        services._check_operation(operation)
        services._operations.add(operation)
        directory = services.root / "private/maps" / content_hash(operation).split(":")[1]
        plan = {
            "run_id": services.run_id,
            "config_hash": services.recipe.config_hash,
            "lock_hash": services.lock_hash,
            **identity,
        }
        plan_path = directory / "plan.json"
        resumed = plan_path.exists()
        if resumed:
            require(_load(plan_path) == read_json(canonical_json(plan)), "map plan changed")
        else:
            _write(plan_path, plan)
        started = time.monotonic()
        child_ids = {job.key: self.child_id(operation, job.key) for job in jobs}
        require(
            not set(child_ids.values()).intersection(services._operations),
            "map child operation collides with an existing operation",
        )
        roles = {job.key: specs[job.stage].role for job in jobs}
        paths = {job.key: directory / child_ids[job.key] for job in jobs}
        completed = {}
        submitted = set()
        for job in jobs:
            request = paths[job.key] / "request.json"
            result = paths[job.key] / "result.json"
            if request.exists():
                require(
                    _load(request) == self._request(plan, job, child_ids[job.key]),
                    "map branch request changed",
                )
                submitted.add(job.key)
            require(not result.exists() or request.exists(), "orphan map branch result")
            if result.exists():
                completed[job.key] = self._read_result(paths[job.key], job, child_ids[job.key])
        # A process crash can leave several independently dispatched actors. Do
        # not launch any new branch until all existing effects are accounted for.
        entries = services.ledger.entries()
        for entry in entries:
            metadata = entry["identity"]["metadata"]
            if entry["state"] == "started" and metadata.get("operation_id") in child_ids.values():
                raise RecoveryRequired(
                    f"map actor {entry['id']} was dispatched without a settled result"
                )
            if entry["state"] in ("started", "reserved") and entry["identity"]["slots"]:
                require(
                    metadata.get("operation_id") in child_ids.values(),
                    "map cannot dispatch while another operation holds unresolved concurrency slots",
                )
        # Recover submitted branches first. Results are joined in original job
        # order, independent of completion order, role caps or resume scheduling.
        pending = [job for job in jobs if job.key in submitted and job.key not in completed]
        pending += [job for job in jobs if job.key not in submitted]
        failed = any(
            row["result"].status != OperationStatus.COMPLETED for row in completed.values()
        )
        stop = failed and identity["failure_policy"] == "fail_fast"
        errors = []
        limit = min(identity["max_workers"], services.recipe.budget.max_concurrency)
        counts = Counter()
        active = {}
        # The parent journal remains the sole writer of the complete run state.
        # Branch receipts are separate immutable files; the actor ledger remains
        # authoritative even if a process exits before a branch receipt is saved.
        with ThreadPoolExecutor(max_workers=limit, thread_name_prefix="proteus-stage") as pool:
            while pending or active:
                while len(active) < limit:
                    eligible = next(
                        (
                            job
                            for job in pending
                            if not errors
                            and (not stop or job.key in submitted)
                            and counts[roles[job.key]]
                            < services.recipe.actors[roles[job.key]].budget.max_concurrency
                        ),
                        None,
                    )
                    if eligible is None:
                        break
                    pending.remove(eligible)
                    key = eligible.key
                    _write(
                        paths[key] / "request.json", self._request(plan, eligible, child_ids[key])
                    )
                    recovering = key in submitted
                    submitted.add(key)
                    values = {
                        name: services.values.get(ref) for name, ref in eligible.inputs.items()
                    }
                    future = pool.submit(
                        self._branch, eligible, child_ids[key], values, paths[key], recovering
                    )
                    active[future] = eligible
                    counts[roles[key]] += 1
                if not active:
                    break
                done, _ = wait(active, return_when=FIRST_COMPLETED)
                for future in done:
                    job = active.pop(future)
                    counts[roles[job.key]] -= 1
                    try:
                        completed[job.key] = future.result()
                        if completed[job.key]["result"].status != OperationStatus.COMPLETED:
                            stop = stop or identity["failure_policy"] == "fail_fast"
                    except BaseException as exc:
                        # Do not classify interrupted/unsaved work as zero-cost
                        # failure. Drain already submitted work and keep the map
                        # journal pending for inspection/recovery.
                        errors.append(exc)
                        stop = True
        self._join(jobs, child_ids, completed)
        if errors:
            raise RecoveryRequired(
                f"map branch did not commit: {type(errors[0]).__name__}: {errors[0]}"
            )
        cancelled = tuple(job.key for job in jobs if job.key not in completed)
        require(
            all(key not in submitted for key in cancelled),
            "submitted branch cannot be silently cancelled",
        )
        result = StageMapResult(
            operation_id=operation,
            keys=tuple(job.key for job in jobs if job.key in completed),
            results=tuple(completed[job.key]["result"] for job in jobs if job.key in completed),
            cancelled=cancelled,
            resumed=resumed,
            wall_seconds=None if resumed else time.monotonic() - started,
        )
        join_path = directory / "join.json"
        if join_path.exists():
            saved = StageMapResult.from_dict(_load(join_path))
            require(
                (saved.operation_id, saved.keys, saved.results, saved.cancelled)
                == (result.operation_id, result.keys, result.results, result.cancelled),
                "saved map join differs",
            )
            return saved
        _write(join_path, result.to_dict())
        return result

    @staticmethod
    def _request(plan, job, operation):
        return {"plan_hash": content_hash(plan), "job": job.to_dict(), "operation_id": operation}

    def _branch(self, job, operation, values, directory, recovering):
        parent = self.services
        branch = _Branch(
            run_id=parent.run_id,
            recipe=parent.recipe,
            registry=parent.registry,
            root=parent.root,
            prompts=parent.prompts,
            resume=recovering,
            lock_hash=parent.lock_hash,
        )
        branch._ledger = parent.ledger
        branch._worker_id = operation
        branch._journal = SimpleNamespace(recovering=recovering)
        for name, value in values.items():
            require(
                branch.values.publish(job.inputs[name].kind, value) == job.inputs[name],
                "branch input reference differs",
            )
        try:
            result = branch._stage(job.stage, job.inputs, operation_id=operation)
        except StageFailed as exc:
            result = exc.result
        except RecoveryRequired:
            raise
        except Exception as exc:
            # Ordinary constructor failures are known outcomes. Interruptions
            # propagate without a branch receipt or permission to retry actors.
            spec = next(s for s in parent.recipe.stages if s.id == job.stage)
            if not branch.requests:
                branch.requests.append(
                    StageRequest(
                        operation_id=operation,
                        attempt_id="attempt-1",
                        run_id=parent.run_id,
                        stage=spec.operator.plugin,
                        role=spec.role,
                        inputs=job.inputs,
                        read_scopes=spec.read_scopes,
                        write_scopes=spec.write_scopes,
                        budget=spec.budget,
                    )
                )
            request = branch.requests[-1]
            first = next(
                (
                    i
                    for i, call in enumerate(branch.actor_calls)
                    if call["attempt_id"] == request.attempt_id
                ),
                len(branch.actor_calls),
            )
            result = StageResult(
                operation_id=operation,
                attempt_id=request.attempt_id,
                run_id=parent.run_id,
                status=OperationStatus.FAILED,
                usage=branch._usage(first),
                diagnostic=f"{type(exc).__name__}: {exc}",
            )
            branch.results.append(result)
        # Actor/system interruption deliberately propagates without a branch
        # result; settled actor receipts will be recovered, not called again.
        record = {
            "request_hash": content_hash(_load(directory / "request.json")),
            "result": result.to_dict(),
            "requests": [r.to_dict() for r in branch.requests],
            "results": [r.to_dict() for r in branch.results],
            "values": branch.values.export(),
            "actor_calls": branch.actor_calls,
            "actor_ordinals": [[key, value] for key, value in branch._actor_ordinals.items()],
        }
        require(
            not branch.candidates
            and not branch.rollouts
            and not branch.evaluations
            and not branch._evaluation_service.sessions
            and branch._builds is None,
            "parallel stage mutated method-owned services",
        )
        _write(directory / "result.json", record)
        return self._read_result(directory, job, operation)

    def _read_result(self, directory, job, operation):
        from proteus.core.methods import Values

        record = _load(directory / "result.json")
        require(
            record["request_hash"] == content_hash(_load(directory / "request.json")),
            "map result/request mismatch",
        )
        result = StageResult.from_dict(record["result"])
        requests = [StageRequest.from_dict(row) for row in record["requests"]]
        results = [StageResult.from_dict(row) for row in record["results"]]
        require(
            requests and len(requests) == len(results) and result == results[-1],
            "map stage attempts are incomplete",
        )
        spec = next(s for s in self.services.recipe.stages if s.id == job.stage)
        for index, (request, outcome) in enumerate(zip(requests, results), 1):
            require(
                request.operation_id == outcome.operation_id == operation
                and request.attempt_id == outcome.attempt_id == f"attempt-{index}"
                and request.run_id == outcome.run_id == self.services.run_id
                and request.inputs == job.inputs
                and request.stage == spec.operator.plugin
                and request.role == spec.role,
                "map stage attempt identity differs",
            )
        values = Values(self.services.registry)
        values.restore(record["values"])
        for ref in (
            *job.inputs.values(),
            *(r for outcome in results for r in outcome.outputs.values()),
        ):
            values.get(ref)
        for call in record["actor_calls"]:
            entry = self.services.ledger.entry(call["id"])
            require(
                entry["settlement"] is not None
                and entry["settlement"]["result"] == call
                and call["operation_id"] == operation,
                "map actor record differs from its settled ledger receipt",
            )
        entries = [
            entry
            for entry in self.services.ledger.entries()
            if entry["identity"]["metadata"].get("operation_id") == operation
        ]
        require(
            {entry["id"] for entry in entries} == {call["id"] for call in record["actor_calls"]}
            and len(record["actor_calls"]) == len(entries),
            "map actor receipt coverage differs",
        )
        return {
            **record,
            "result": result,
            "requests": requests,
            "results": results,
            "typed_values": values,
        }

    def _join(self, jobs, child_ids, completed):
        services = self.services
        for job in jobs:
            if job.key not in completed:
                continue
            row = completed[job.key]
            require(
                child_ids[job.key] not in services._operations, "map child operation already joined"
            )
            services._operations.add(child_ids[job.key])
            for ref, value in row["typed_values"]._values.values():
                require(
                    services.values.publish(ref.kind, value) == ref, "map output reference differs"
                )
            services.requests.extend(row["requests"])
            services.results.extend(row["results"])
            services.actor_calls.extend(row["actor_calls"])
            for key, ordinal in row["actor_ordinals"]:
                require(
                    key[0] == child_ids[job.key],
                    "branch actor ordinal belongs to another operation",
                )
                services._actor_ordinals[tuple(key)] = ordinal
