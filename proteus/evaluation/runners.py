"""Local execution adapters and a task grader. Container isolation belongs to P04.

The local provider executes trusted code on the host. It supports explicit JSON state
and fresh working trees, and rejects settings it cannot enforce (including network
restrictions). Task references are supplied exclusively to the evaluator.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

from proteus.core.contracts import (
    TEXT, OperationStatus, Outcome, PluginRef, RuntimeIdentity, SessionPolicy, StatePolicy, Usage, require,
)
from proteus.core.serialization import Contract, canonical_json, content_hash
from proteus.evaluation.records import Feedback, Grade, SubjectResult, Trace, zero_usage
from proteus.evaluation.tasks import StateValue, Task


@dataclass(frozen=True)
class EnvironmentLease:
    loaded_artifact_hash: str
    local_root: Path | None = None
    handle: Any = None
    code_executor: Any = None


class Environment(Protocol):
    def open(self, artifact, store, root, execution): ...


class SubjectRunner(Protocol):
    def run(self, context: SubjectContext) -> SubjectResult: ...


class InvalidCandidate(ValueError):
    """The pinned candidate cannot execute under this runner's input contract."""


class Evaluator(Protocol):
    def grade(self, task: Task, rollout, reference: Any) -> Grade: ...


@dataclass(frozen=True)
class SubjectContext:
    candidate: Any
    task: Task
    state: StateValue
    history: tuple
    seed: int
    execution: Any
    subject: Any
    lease: EnvironmentLease
    _actor: Any = None
    inputs: Mapping[str, Contract] = field(default_factory=dict)
    actor_runtime: PluginRef | None = None
    role_spec: Any = None
    _role_runtime: Any = None

    def actor(self, prompt, *, timeout_seconds=None):
        require(self._actor is not None, "subject has no managed actor role")
        return self._actor(prompt, timeout_seconds=timeout_seconds) if timeout_seconds is not None else self._actor(prompt)


@dataclass(frozen=True)
class SubjectRecoveryContext:
    """Identity and private records of one existing dispatch; no execution lease.

    A subject.reconcile plugin must inspect that dispatch, never start a replacement.
    Return its completed SubjectResult or raise RecoveryRequired when completion
    cannot be established. Candidate/runtime validation still belongs to the service.
    """

    candidate: Any
    task: Task
    state: StateValue
    history: tuple
    seed: int
    execution: Any
    subject: Any
    root: Path
    inputs: Mapping[str, Contract] = field(default_factory=dict)


class LocalJsonEnvironment:
    @contextmanager
    def open(self, artifact, store, root, execution):
        from proteus.execution.code_rpc import invoke
        materialized = store.materialize(artifact, root / "candidate")
        def execute(options, call, actor):
            return invoke(materialized, options, call, actor, timeout_seconds=execution.timeout_seconds)
        yield EnvironmentLease(loaded_artifact_hash=artifact.content_hash, local_root=materialized,
                               code_executor=execute)


def validate_local(execution, options):
    environment = execution.environment
    require(environment.provider == "host" and environment.network == "unrestricted",
            "local JSON execution requires provider=host and explicit unrestricted host networking")
    require(not environment.resources and not environment.allowed_hosts and not environment.read_scopes
            and not environment.write_scopes and environment.image_digest is None
            and environment.build_hash is None,
            "local JSON execution cannot enforce container/resource/filesystem restrictions")
    require(execution.state_policy in (StatePolicy.INDEPENDENT, StatePolicy.SEQUENCE),
            "local JSON execution supports independent or explicit JSON sequence state")


@dataclass(frozen=True, kw_only=True)
class PythonSubjectOptions(Contract):
    entrypoint: str = field(default="main.py", metadata=TEXT)


def validate_python(recipe, options):
    from proteus.core.contracts import ArtifactComponent
    require(os.name != "nt", "local Python subjects currently require POSIX process-group cleanup")
    require(recipe.subject.actor is None, "Python subject executes candidate code without a managed actor")
    require(recipe.subject.initial_artifact.codec.name == "proteus.file_tree",
            "Python subject requires a file-tree artifact")
    ArtifactComponent(path=options.entrypoint, content_hash=content_hash("path-check"), size_bytes=0)
    require(not recipe.subject.mutable_scopes, "local Python subject state is explicit JSON, not filesystem inheritance")
    require(recipe.execution is not None and recipe.execution.environment.backend.plugin.name == "proteus.local_json",
            "Python subject requires the local JSON environment")
    require(recipe.budget.max_model_calls is None,
            "arbitrary candidate code has unknown model usage; it cannot enforce a finite model-call cap")


@dataclass(frozen=True, kw_only=True)
class PythonReply(Contract):
    output: Any
    state: StateValue = field(default_factory=StateValue)
    status: Outcome = Outcome.SUCCEEDED
    trace: Trace = field(default_factory=Trace)


class PythonSubject:
    def __init__(self, options):
        self.options = options

    def run(self, context):
        started = time.monotonic()
        root = context.lease.local_root
        require(root is not None, "Python subject needs a local materialization")
        runtime = RuntimeIdentity(runtime=context.subject.runner.plugin,
            settings={"executable": sys.executable, "entrypoint": self.options.entrypoint},
            environment_hash=content_hash(context.execution.environment))

        def result(status, *, output=None, state=None, trace=None, diagnostic=""):
            return SubjectResult(loaded_artifact_hash=context.lease.loaded_artifact_hash, runtime=runtime,
                status=status, output=output, state=state or context.state, trace=trace or Trace(),
                usage=Usage(wall_seconds=time.monotonic() - started), diagnostic=diagnostic)

        if self.options.entrypoint not in {item.path for item in context.candidate.artifact.components}:
            return result(Outcome.INVALID_CANDIDATE, diagnostic="candidate entrypoint is missing")
        payload = canonical_json({"task": context.task, "state": context.state,
                                  "history": context.history, "seed": context.seed})
        # A new interpreter/tree on every step: only explicit state/history are inherited.
        process = subprocess.Popen([sys.executable, "-E", self.options.entrypoint], cwd=root,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace", start_new_session=os.name != "nt")
        def terminate():
            if os.name != "nt":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                process.kill()
        try:
            stdout, stderr = process.communicate(payload, timeout=context.execution.timeout_seconds)
        except subprocess.TimeoutExpired:
            terminate()
            stdout, stderr = process.communicate()
            return result(Outcome.TIMED_OUT, trace=Trace(events=({"stdout": stdout, "stderr": stderr},)),
                          diagnostic="subject execution timed out")
        except BaseException:
            terminate()
            process.communicate()
            raise
        trace = Trace(events=({"stdout": stdout, "stderr": stderr, "exit_code": process.returncode},))
        if process.returncode != 0:
            return result(Outcome.INVALID_CANDIDATE, trace=trace, diagnostic="candidate process exited unsuccessfully")
        try:
            reply = PythonReply.from_json(stdout)
        except (ValueError, TypeError) as exc:
            return result(Outcome.INVALID_CANDIDATE, trace=trace, diagnostic=f"invalid candidate reply: {exc}")
        return result(reply.status, output=reply.output, state=reply.state,
                      trace=Trace(events=(*trace.events, *reply.trace.events)))


@dataclass(frozen=True, kw_only=True)
class PromptSubjectOptions(Contract):
    component: str = field(default="system.txt", metadata=TEXT)


def validate_prompt(recipe, options):
    require(recipe.subject.actor is not None, "prompt subject needs an actor role")
    require(recipe.subject.initial_artifact.codec.name == "proteus.text_components",
            "prompt subject requires text components")
    require(recipe.execution is not None and recipe.execution.environment.backend.plugin.name == "proteus.local_json",
            "prompt subject requires the local JSON environment")
    require(recipe.execution.timeout_seconds is None,
            "prompt subject uses its actor transport timeout; an execution deadline is not implemented")
    require(not recipe.subject.mutable_scopes, "prompt subject state is explicit JSON")
    require(recipe.actors[recipe.subject.actor].session == SessionPolicy.FRESH,
            "prompt subject uses fresh requests and explicit history")


class PromptSubject:
    def __init__(self, options):
        self.options = options

    def run(self, context):
        if self.options.component not in {item.path for item in context.candidate.artifact.components}:
            raise InvalidCandidate("candidate prompt component is missing")
        require(context.lease.local_root is not None, "prompt subject needs materialized text components")
        instructions = (context.lease.local_root / self.options.component).read_text(encoding="utf-8")
        prompt = instructions + "\n\n" + canonical_json({"task": context.task, "state": context.state,
                                                        "history": context.history, "seed": context.seed})
        answer = context.actor(prompt)
        return SubjectResult(loaded_artifact_hash=context.lease.loaded_artifact_hash,
            runtime=replace(answer.runtime, environment_hash=content_hash(context.execution.environment)),
            status=Outcome.SUCCEEDED if answer.status == OperationStatus.COMPLETED else Outcome.INFRA_ERROR,
            output=answer.text, state=context.state, usage=answer.usage, trace=Trace(events=answer.trace),
            diagnostic=answer.diagnostic)


class ExactMatch:
    def grade(self, task, rollout, reference):
        matches = canonical_json(rollout.output) == canonical_json(reference)
        return Grade(status=Outcome.SUCCEEDED if matches else Outcome.TASK_FAILED,
            score=float(matches), metrics={"exact_match": float(matches)},
            feedback=Feedback(value={"correct": matches, "reference": reference}), usage=zero_usage())
