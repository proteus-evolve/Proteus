"""Standalone worker launched by an explicitly configured Python 3.12+ runtime.

No Proteus or Harbor imports are required in the controller's Python environment.
Requests/results and all Harbor records live in a private trial directory. This
module is executed as a script, not registered as a public data/actor plugin.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import shlex
import signal
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path


HARBOR_VERSION = "0.24.0"
HARBOR_REVISION = "b53b8134e1241686dca7759af188f987ecc48e8b"
# All 424 packaged files were verified byte-identical to src/harbor at this commit.
HARBOR_SOURCE_HASH = "sha256:7e816d87f2cb72055ca539247d66c94799fde294a6178411f72d8613f092b72f"


def canonical(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return "sha256:" + hashlib.sha256(canonical(value).encode()).hexdigest()


def runtime_identity():
    import harbor
    package = Path(harbor.__file__).parent
    files = {p.relative_to(package).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in package.rglob("*") if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"}
    version, source_hash = importlib.metadata.version("harbor"), digest(files)
    if version != HARBOR_VERSION or source_hash != HARBOR_SOURCE_HASH:
        raise ValueError("Harbor runtime does not match the pinned version/source bytes")
    packages = sorted((d.metadata["Name"].lower().replace("_", "-"), d.version)
                      for d in importlib.metadata.distributions())
    identity = {"harbor_version": version, "harbor_revision": HARBOR_REVISION,
                "harbor_source_hash": source_hash, "python": platform.python_version(),
                "platform": platform.system() + "-" + platform.machine(), "packages": packages}
    return {**identity, "runtime_hash": digest(identity)}


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".pending")
    with temporary.open("w", encoding="utf-8") as stream:
        stream.write(canonical(value) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def docker(*args, timeout=30):
    process = subprocess.Popen(["docker", *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except BaseException:
        # Docker Desktop can spawn a wrapper child retaining the pipes. Killing
        # just the CLI parent would leave communicate() waiting indefinitely.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate(timeout=5)
        raise
    if process.returncode:
        raise subprocess.CalledProcessError(process.returncode, process.args, stdout, stderr)
    return stdout.strip()


def cleanup_owned_resources(trial_name, *, timeout=45):
    """Reconcile only this generated trial's exact Compose labels; never prune.

    Called after the worker process group has stopped, also when Harbor's own
    cleanup failed. A daemon timeout is an unverified cleanup, not success.
    """
    if re.fullmatch(r"proteus-[0-9a-f]{32}", trial_name) is None:
        raise ValueError("invalid owned trial name")
    deadline = time.monotonic() + timeout
    records, errors = [], []
    def command(*args):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("cleanup deadline exceeded")
        return docker(*args, timeout=min(10, remaining))
    for project in (trial_name + "__env", trial_name + "__verifier__trial"):
        for kind, listing, removing in (
            ("container", ("ps", "--all", "--quiet"), ("rm", "--force", "--volumes")),
            ("network", ("network", "ls", "--quiet"), ("network", "rm")),
            ("volume", ("volume", "ls", "--quiet"), ("volume", "rm")),
        ):
            try:
                query = (*listing, "--filter", "label=com.docker.compose.project=" + project)
                ids = command(*query).splitlines()
                if ids:
                    command(*removing, *ids)
                remaining = command(*query).splitlines()
                records.append({"project": project, "kind": kind, "removed": ids, "remaining": remaining})
            except Exception as exc:
                errors.append({"project": project, "kind": kind, "error": type(exc).__name__})
    return {"verified": not errors and all(not r["remaining"] for r in records),
            "resources": records, "errors": errors}


def inspect_environment(environment):
    # The session ID is a generated lowercase Docker Compose project identifier.
    project = environment.session_id
    ids = docker("ps", "--all", "--filter", f"label=com.docker.compose.project={project}",
                 "--filter", "label=com.docker.compose.service=main", "--format", "{{.ID}}").splitlines()
    if len(ids) != 1:
        raise ValueError("Harbor environment does not resolve to exactly one main container")
    row = json.loads(docker("inspect", ids[0]))[0]
    # Never serialize Env, Docker auth data or other containers' configuration.
    return {"container_id": row["Id"], "image_id": row["Image"], "project": project,
            "network_mode": row["HostConfig"]["NetworkMode"],
            "nano_cpus": row["HostConfig"]["NanoCpus"], "memory_bytes": row["HostConfig"]["Memory"],
            "mount_destinations": sorted(m["Destination"] for m in row["Mounts"])}


async def checked_environment(request, environment, mounts):
    actual = await asyncio.to_thread(inspect_environment, environment)
    expected_image = json.loads(await asyncio.to_thread(docker, "image", "inspect", request["image_digest"]))[0]["Id"]
    if actual["image_id"] != expected_image:
        raise ValueError("container image differs from the pinned image")
    resources = request["resources"]
    if (("cpus" in resources and actual["nano_cpus"] != resources["cpus"] * 10**9)
            or ("memory_mb" in resources and actual["memory_bytes"] != resources["memory_mb"] * 1024**2)):
        raise ValueError("Docker did not apply the requested resource limits")
    if set(actual["mount_destinations"]) != mounts:
        raise ValueError("unexpected container mounts, including possible verifier exposure")
    return actual


# Executed inside the container before any candidate code. The witness is read
# into the host adapter's memory; a candidate cannot replace it by editing logs.
VERIFY_TREE = r'''
import base64, hashlib, json, os, stat, sys
from pathlib import Path
ref=json.loads(base64.b64decode(sys.argv[1])); root=Path('/opt/proteus/candidate')
actual=[]
for p in sorted(root.rglob('*')):
    mode=p.lstat().st_mode
    if stat.S_ISDIR(mode): continue
    if not stat.S_ISREG(mode): raise ValueError('candidate contains a non-regular file')
    data=p.read_bytes()
    actual.append({'schema_version':1,'path':p.relative_to(root).as_posix(),
        'content_hash':'sha256:'+hashlib.sha256(data).hexdigest(),'size_bytes':len(data),'executable':bool(mode&0o111)})
actual.sort(key=lambda x:x['path'])
if actual != ref['components']: raise ValueError('loaded candidate components differ')
expected=ref.pop('content_hash')
ref['components']=actual
loaded='sha256:'+hashlib.sha256(json.dumps(ref,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
if loaded != expected: raise ValueError('loaded candidate manifest differs')
print(json.dumps({'loaded_artifact_hash':loaded,'components':actual}))
'''


def candidate_agent_class():
    from harbor.agents.base import BaseAgent
    from harbor.agents.installed.base import NonZeroAgentExitCodeError

    class CandidateAgent(BaseAgent):
        @staticmethod
        def name():
            return "proteus-candidate"

        def version(self):
            return "1"

        def __init__(self, proteus_request, **kwargs):
            super().__init__(**kwargs)
            self.request = proteus_request
            self.loaded_proof = None
            self.execution = None

        async def setup(self, environment):
            await environment.upload_dir(source_dir=Path(self.request["candidate_dir"]),
                                         target_dir="/opt/proteus/candidate")
            encoded = base64.b64encode(canonical(self.request["artifact"]).encode()).decode()
            result = await environment.exec(shlex.join(["python3", "-c", VERIFY_TREE, encoded]), timeout_sec=30)
            if result.return_code:
                raise ValueError("container candidate verification failed")
            proof = json.loads(result.stdout)
            actual_environment = await checked_environment(self.request, environment, {"/logs/agent", "/logs/artifacts"})
            self.loaded_proof = {**proof, **actual_environment,
                "candidate_id": self.request["candidate_id"], "task_hash": self.request["task_hash"],
                "requested_image": self.request["image_digest"]}
            write_json(self.logs_dir / "proteus-loaded.json", self.loaded_proof)

        async def run(self, instruction, environment, context):
            payload = {"task": {**self.request["task"], "input": {"instruction": instruction}},
                       "state": self.request["state"], "history": [], "seed": self.request["seed"]}
            input_path = self.logs_dir / "proteus-input.json"
            write_json(input_path, payload)
            await environment.upload_file(source_path=input_path, target_path="/opt/proteus/input.json")
            command = shlex.join(["python3", "/opt/proteus/candidate/" + self.request["entrypoint"],
                                  *self.request["arguments"]]) + " < /opt/proteus/input.json"
            result = await environment.exec(command, cwd="/opt/proteus/candidate",
                env={key: os.environ[key] for key in self.request["credential_env"]})
            self.execution = {"stdout": result.stdout or "", "stderr": result.stderr or "",
                              "return_code": result.return_code}
            write_json(self.logs_dir / "proteus-execution.json", self.execution)
            context.metadata = {"proteus_loaded_artifact": self.loaded_proof["loaded_artifact_hash"]}
            # Arbitrary candidate programs do not have a defensible token/call count.
            if result.return_code:
                raise NonZeroAgentExitCodeError(result.return_code)

    return CandidateAgent


async def run(request, output):
    from harbor.models.trial.config import AgentConfig, EnvironmentConfig, TaskConfig, TrialConfig
    from harbor.trial.single_step import SingleStepTrial
    from harbor.trial.trial import Trial

    globals()["CandidateAgent"] = candidate_agent_class()
    identity = runtime_identity()
    if identity["runtime_hash"] != request["runtime_hash"]:
        raise ValueError("Harbor runtime fingerprint changed")
    class PrivateVerifierTrial(SingleStepTrial):
        @property
        def _agent_env_mounts(self):
            # Harbor 0.24.0 also mounts verifier logs into separate-mode agents.
            # Keep those host files inaccessible to candidate processes.
            return [m for m in super()._agent_env_mounts
                    if m["target"] != str(self.agent_env_paths.verifier_dir)]

        @asynccontextmanager
        async def _separate_verifier_env(self, definition, **kwargs):
            async with super()._separate_verifier_env(definition, **kwargs) as environment:
                proof = await checked_environment(request, environment, {"/logs/verifier"})
                if self.agent.loaded_proof and proof["container_id"] == self.agent.loaded_proof["container_id"]:
                    raise ValueError("verifier reused the candidate container")
                self.verifier_proof = proof
                yield environment

    config = TrialConfig(task=TaskConfig(path=Path(request["task_dir"])),
        trial_name=request["trial_name"], trials_dir=Path(request["trials_dir"]),
        agent=AgentConfig(import_path="__main__:CandidateAgent", model_name=request["model"],
            override_timeout_sec=request["timeout_seconds"],
            override_setup_timeout_sec=request["setup_timeout_seconds"], kwargs={"proteus_request": request}),
        environment=EnvironmentConfig(type="docker", delete=True))
    # Trial.create selects a concrete class itself; instantiate the pinned
    # single-step implementation explicitly so the private mount policy applies.
    task, download = await Trial._load_task(config)
    trial = PrivateVerifierTrial(config, _task=task, _task_download_result=download)
    trial.verifier_proof = None
    # Persist the UUID before starting any container or external subject work.
    write_json(output.parent / "harbor-started.json", {"trial_id": str(trial.id),
        "trial_name": request["trial_name"], "candidate_id": request["candidate_id"],
        "artifact_hash": request["artifact"]["content_hash"], "task_hash": request["task_hash"]})
    try:
        await trial.run()
    finally:
        # Preserve phase usage and verifier evidence even on cooperative cancellation.
        write_json(output, {"runtime": identity, "result": trial.result.model_dump(mode="json"),
            "loaded_proof": trial.agent.loaded_proof, "verifier_proof": trial.verifier_proof,
            "execution": trial.agent.execution})


def main():
    if sys.argv[1:] == ["identify"]:
        print(canonical(runtime_identity()))
        return
    if len(sys.argv) != 4 or sys.argv[1] != "run":
        raise SystemExit("usage: harbor_worker.py identify | run REQUEST OUTPUT")
    request_path, output = Path(sys.argv[2]), Path(sys.argv[3])
    try:
        asyncio.run(run(json.loads(request_path.read_text()), output))
    except BaseException as exc:
        write_json(output.parent / "bridge-error.json", {"type": type(exc).__name__, "message": str(exc)})
        raise


if __name__ == "__main__":
    main()
