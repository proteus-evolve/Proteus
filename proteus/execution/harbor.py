"""Controller-side Harbor bridge; Harbor runs in its own pinned Python runtime."""

from __future__ import annotations

import os
import signal
import subprocess
import time
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from proteus.artifacts.store import ArtifactStore, publish_bytes
from proteus.core.contracts import DIGEST, TEXT, ArtifactComponent, Outcome, RuntimeIdentity, StatePolicy, Usage, require
from proteus.core.serialization import Contract, canonical_json, content_hash, read_json
from proteus.evaluation.records import BackendEvaluation, Feedback, Grade, SubjectResult, Trace, zero_usage
from proteus.evaluation.runners import EnvironmentLease, PythonReply
from proteus.execution.harbor_worker import cleanup_owned_resources


WORKER = Path(__file__).with_name("harbor_worker.py")
BACKEND_CAPABILITY = "evaluation.backend.harbor"


@dataclass(frozen=True, kw_only=True)
class HarborEnvironmentOptions(Contract):
    python: str = field(metadata=TEXT)
    runtime_hash: str = field(metadata=DIGEST)
    setup_timeout_seconds: float = field(default=120, metadata={"exclusiveMinimum": 0})
    bridge_timeout_seconds: float = field(default=600, metadata={"exclusiveMinimum": 0})


@dataclass(frozen=True, kw_only=True)
class HarborSubjectOptions(Contract):
    entrypoint: str = field(default="main.py", metadata=TEXT)
    arguments: tuple[str, ...] = ()
    model: str | None = field(default=None, metadata=TEXT)
    credential_env: tuple[str, ...] = ()


@dataclass(frozen=True, kw_only=True)
class HarborRewardOptions(Contract):
    reward_key: str = field(default="reward", metadata=TEXT)


@dataclass(frozen=True, kw_only=True)
class HarborTask(Contract):
    path: str = field(metadata=TEXT)
    tree_hash: str = field(metadata=DIGEST)


def identify_runtime(python: str):
    result = subprocess.run([python, "-I", str(WORKER), "identify"], capture_output=True,
                            text=True, timeout=60, check=True)
    return read_json(result.stdout)


def validate_environment(execution, options):
    env = execution.environment
    require(os.name != "nt", "Harbor bridge currently requires POSIX worker process-group cleanup")
    require(Path(options.python).is_absolute(), "Harbor Python must be an explicit absolute path")
    require(env.provider == "docker" and env.image_digest is not None,
            "Harbor bridge requires Docker and an immutable image_digest")
    require(env.build_hash is None, "Harbor build recipes require the subsequent source-build binding")
    require(execution.state_policy == StatePolicy.INDEPENDENT,
            "this Harbor adapter currently implements independent trials")
    require(execution.timeout_seconds is not None, "Harbor subjects require an explicit execution timeout")
    require(not env.read_scopes and not env.write_scopes,
            "Harbor filesystem scopes must be implemented by the task environment, not silently ignored")
    require(set(env.resources) <= {"cpus", "memory_mb"}, "unsupported Harbor resource declaration")
    require(all(type(v) is int and v > 0 for v in env.resources.values()), "resources must be positive integers")


def validate_subject(recipe, options):
    require(recipe.subject.actor is None, "Harbor executes candidate code; it is not a managed actor proxy")
    require(recipe.subject.initial_artifact.codec.name == "proteus.file_tree", "Harbor code subject needs a file tree")
    require(recipe.execution is not None
            and recipe.execution.environment.backend.plugin.name == "proteus.harbor",
            "Harbor subject requires the Harbor environment")
    require(not recipe.subject.mutable_scopes, "Harbor trial state cannot inherit candidate filesystem mutations")
    require(recipe.budget.max_model_calls is None, "arbitrary Harbor code cannot enforce a finite model-call cap")
    require(all(spec.evaluator.plugin.name == "proteus.harbor_reward" for spec in recipe.evaluations),
            "Harbor trial rewards require the Harbor verifier adapter")
    ArtifactComponent(path=options.entrypoint, content_hash=content_hash("path"), size_bytes=0)
    import re
    require(all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) for key in options.credential_env),
            "credential references must be environment variable names")


@dataclass(frozen=True)
class HarborLease:
    root: Path
    store: ArtifactStore
    options: HarborEnvironmentOptions


class HarborEnvironment:
    def __init__(self, options):
        self.options = options

    @contextmanager
    def open(self, artifact, store, root, execution):
        identity = identify_runtime(self.options.python)
        require(identity["runtime_hash"] == self.options.runtime_hash, "Harbor runtime fingerprint changed")
        candidate = store.materialize(artifact, root / "candidate")
        yield EnvironmentLease(loaded_artifact_hash=artifact.content_hash, local_root=candidate,
                               handle=HarborLease(root, store, self.options))


def _duration(value):
    if not value or not value.get("started_at") or not value.get("finished_at"):
        return None
    return max(0, (datetime.fromisoformat(value["finished_at"]) - datetime.fromisoformat(value["started_at"])).total_seconds())


def _usage(result):
    agent = result.get("agent_result") or {}
    return Usage(input_tokens=agent.get("n_input_tokens"), output_tokens=agent.get("n_output_tokens"),
                 cost_usd=agent.get("cost_usd"), wall_seconds=_duration(result.get("agent_execution")))


def _verifier_usage(result):
    timing = result.get("verifier")
    if not timing or not timing.get("started_at"):
        return zero_usage()
    # A task's arbitrary verifier script can call models; elapsed time alone
    # does not justify recording zero tokens/cost/calls.
    return Usage(wall_seconds=_duration(timing))


def _task_config(task_root, execution):
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib
    config = tomllib.loads((task_root / "task.toml").read_text(encoding="utf-8"))
    require(not config.get("steps"), "this Harbor adapter requires a single-step task")
    env, verifier = config.get("environment", {}), config.get("verifier", {})
    required = execution.environment
    require(env.get("docker_image") == required.image_digest, "task image differs from the execution pin")
    modes = {"none": "no-network", "restricted": "allowlist", "unrestricted": "public"}
    require(env.get("network_mode", "public") == modes[required.network], "task network policy differs")
    require(set(env.get("allowed_hosts", ())) == set(required.allowed_hosts), "task network allowlist differs")
    for part in ("agent", "verifier"):
        phase = config.get(part, {})
        require(phase.get("network_mode", modes[required.network]) == modes[required.network]
                and set(phase.get("allowed_hosts", required.allowed_hosts)) == set(required.allowed_hosts),
                "task phase network policy differs from execution")
    require(all(env.get(key) == value for key, value in required.resources.items()), "task resources differ")
    require(env.get("os", "linux") == "linux", "this adapter requires a Linux task image with Python 3")
    require(not any(key in env for key in ("allow_internet", "memory", "storage")),
            "deprecated environment fields must be made explicit before execution")
    require(not any(env.get(key) for key in ("storage_mb", "gpus", "gpu_types", "tpu")),
            "task resources are not supported by this Docker bridge")
    require(config.get("agent", {}).get("user") is None,
            "task agent user overrides require explicit subject support")
    require(verifier.get("environment_mode") == "separate", "Harbor recipes must explicitly isolate the verifier")
    require(not verifier.get("environment"), "a distinct verifier image needs a separately pinned environment")
    require(not any(config.get(part, {}).get("env") for part in ("environment", "agent", "verifier", "solution")),
            "task environment interpolation is disabled; declare subject credential_env explicitly")
    require(not env.get("mcp_servers") and not env.get("skills_dir"), "task-injected tools require additional pins")
    require(not (task_root / "environment/docker-compose.yaml").exists(),
            "custom Compose services/mounts require explicit environment validation")
    return config


def _stop_worker(process):
    # Always signal the whole group, including a CLI left behind by an exited worker.
    for sig, timeout in ((signal.SIGINT, 10), (signal.SIGKILL, 5)):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            break
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            continue
        # A worker may exit before its child CLI; still terminate that owned group.
        if sig == signal.SIGINT:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        break


class HarborSubject:
    def __init__(self, options):
        self.options = options

    def run(self, context):
        lease = context.lease.handle
        require(isinstance(lease, HarborLease), "Harbor subject needs its matching lease")
        task = HarborTask.from_dict(context.task.input)
        private = lease.root / "private"
        private.mkdir()
        tasks = ArtifactStore(private / "task-store", lease.store.registry)
        ref = tasks.capture_tree(task.path)
        require(ref.content_hash == task.tree_hash, "Harbor task tree changed")
        task_root = tasks.materialize(ref, private / "task")
        # Empty environment directories are not part of the content manifest.
        (task_root / "environment").mkdir(exist_ok=True)
        _task_config(task_root, context.execution)
        require(self.options.entrypoint in {c.path for c in context.candidate.artifact.components},
                "Harbor candidate entrypoint is missing")
        require(all(key in os.environ for key in self.options.credential_env), "missing subject credential environment")
        trial_name = "proteus-" + uuid.uuid4().hex
        request = {"runtime_hash": lease.options.runtime_hash, "task_dir": str(task_root),
            "trials_dir": str(private / "trials"), "trial_name": trial_name,
            "candidate_dir": str(context.lease.local_root), "candidate_id": context.candidate.id,
            "artifact": context.candidate.artifact, "task_hash": content_hash(context.task),
            "task": context.task, "state": context.state, "seed": context.seed,
            "entrypoint": self.options.entrypoint, "arguments": self.options.arguments,
            "model": self.options.model, "credential_env": self.options.credential_env,
            "timeout_seconds": context.execution.timeout_seconds,
            "setup_timeout_seconds": lease.options.setup_timeout_seconds,
            "image_digest": context.execution.environment.image_digest,
            "resources": context.execution.environment.resources}
        request_path, response_path = private / "request.json", private / "response.json"
        publish_bytes(request_path, canonical_json(request).encode())
        # Only declared credentials and process/Docker plumbing reach the worker.
        keys = {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "DOCKER_HOST", "DOCKER_CONTEXT",
                "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH", "DOCKER_CONFIG", *self.options.credential_env}
        env = {key: value for key, value in os.environ.items() if key in keys}
        started = time.monotonic()
        bridge_failure = False
        with (private / "bridge.log").open("wb") as log:
            process = subprocess.Popen([lease.options.python, "-I", str(WORKER), "run", str(request_path),
                str(response_path)], stdout=log, stderr=subprocess.STDOUT, env=env, start_new_session=True)
            try:
                process.wait(timeout=lease.options.bridge_timeout_seconds)
            except subprocess.TimeoutExpired:
                bridge_failure = True
            finally:
                _stop_worker(process)
                cleanup = cleanup_owned_resources(trial_name)
                publish_bytes(private / "cleanup.json", canonical_json(cleanup).encode())
                publish_bytes(private / "bridge-usage.json", canonical_json(
                    {"elapsed_seconds": time.monotonic() - started, "return_code": process.returncode,
                     "deadline_exceeded": bridge_failure}).encode())
        runtime = RuntimeIdentity(runtime=context.subject.runner.plugin, model=self.options.model,
            environment_hash=content_hash(context.execution.environment),
            settings={"harbor_runtime_hash": lease.options.runtime_hash})
        if not response_path.exists():
            return SubjectResult(loaded_artifact_hash=context.candidate.artifact.content_hash, runtime=runtime,
                status=Outcome.INFRA_ERROR, state=context.state, usage=Usage(wall_seconds=time.monotonic() - started),
                diagnostic="Harbor bridge failed; controller-private records contain diagnostics")
        response = read_json(response_path.read_text())
        require(response["runtime"]["runtime_hash"] == lease.options.runtime_hash, "worker runtime changed")
        raw, proof = response["result"], response["loaded_proof"]
        require(raw["config"]["trial_name"] == trial_name and raw["verifier_environment_mode"] in (None, "separate"),
                "Harbor trial identity/verifier isolation mismatch")
        backend = BackendEvaluation(producer=context.subject.runner.plugin,
            artifact_hash=context.candidate.artifact.content_hash, task_hash=content_hash(context.task),
            trial_id=raw["id"], payload={"raw": raw, "raw_hash": content_hash(raw),
                "task_tree_hash": ref.content_hash, "cleanup": cleanup},
            usage=_verifier_usage(raw))
        status, diagnostic = Outcome.SUCCEEDED, ""
        error = raw.get("exception_info") or {}
        if bridge_failure or process.returncode or not cleanup["verified"]:
            status, diagnostic = Outcome.INFRA_ERROR, "Harbor bridge or resource cleanup did not complete"
        elif error.get("exception_type") == "AgentTimeoutError":
            status, diagnostic = Outcome.TIMED_OUT, "Harbor subject exceeded its execution deadline"
        elif not proof:
            status, diagnostic = Outcome.INFRA_ERROR, "Harbor did not verify a loaded candidate"
        elif response["execution"] is None or response["execution"]["return_code"]:
            status, diagnostic = Outcome.INVALID_CANDIDATE, "Harbor candidate did not produce a valid execution"
        if proof:
            require(proof["loaded_artifact_hash"] == context.candidate.artifact.content_hash
                    and proof["candidate_id"] == context.candidate.id and proof["task_hash"] == content_hash(context.task),
                    "Harbor loaded a different candidate/task")
            resources = context.execution.environment.resources
            require(("cpus" not in resources or proof["nano_cpus"] == resources["cpus"] * 10**9)
                    and ("memory_mb" not in resources or proof["memory_bytes"] == resources["memory_mb"] * 1024**2),
                    "Docker did not apply the requested resource limits")
        execution = response["execution"] or {}
        reply = None
        if status == Outcome.SUCCEEDED:
            try:
                reply = PythonReply.from_json(execution["stdout"])
                status = reply.status
            except (ValueError, TypeError, KeyError):
                status, diagnostic = Outcome.INVALID_CANDIDATE, "Harbor candidate stdout is not a PythonReply"
        return SubjectResult(loaded_artifact_hash=context.candidate.artifact.content_hash, runtime=runtime,
            status=status, output=reply.output if reply else None, state=reply.state if reply else context.state,
            trace=Trace(events=({"harbor_trial_id": raw["id"], "task_checksum": raw["task_checksum"],
                "loaded": proof, "execution": execution},)), usage=_usage(raw), diagnostic=diagnostic,
            backend_evaluation=backend)


class HarborReward:
    def __init__(self, options):
        self.options = options

    def grade(self, task, rollout, reference: Any, *, backend):
        raw = backend.payload["raw"]
        require(content_hash(raw) == backend.payload["raw_hash"] and raw["id"] == backend.trial_id,
                "private Harbor result identity changed")
        error = raw.get("exception_info")
        if error or raw.get("verifier_environment_mode") != "separate":
            return Grade(status=Outcome.INFRA_ERROR, score=None, usage=backend.usage,
                         diagnostic="Harbor verifier/trial did not complete under the declared protocol")
        rewards = (raw.get("verifier_result") or {}).get("rewards")
        if not isinstance(rewards, Mapping) or self.options.reward_key not in rewards:
            return Grade(status=Outcome.SCORE_MISSING, score=None, usage=backend.usage,
                         diagnostic="Harbor result has no declared reward")
        score = rewards[self.options.reward_key]
        return Grade(status=Outcome.SUCCEEDED if score else Outcome.TASK_FAILED, score=score,
            metrics={self.options.reward_key: score}, usage=backend.usage, feedback=Feedback(value={"reward": score}))
