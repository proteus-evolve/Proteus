"""Persistent research workspaces with per-generation pinned Harbor containers."""

from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
from typing import Literal, Mapping

from proteus.core.contracts import DIGEST, TEXT, require
from proteus.core.serialization import Contract, content_hash, read_json
from proteus.execution.builds import image_pin
from proteus.workers.records import CommandWorkerOptions, WorkerObservation
from proteus.workers.runtime import CommandWorkerRuntime, immutable


@dataclass(frozen=True, kw_only=True)
class HarborWorkerOptions(Contract):
    python: str = field(metadata=TEXT)
    runtime_hash: str = field(metadata=DIGEST)
    image_digest: str = field(metadata=TEXT)
    command: tuple[str, ...] = field(metadata={"minItems": 1})
    command_files: Mapping[str, str]
    user: str = field(metadata=TEXT)
    credential_env: tuple[str, ...] = ()
    network: Literal["none", "public"] = "none"
    cpus: int = field(default=1, metadata={"minimum": 1})
    memory_mb: int = field(default=1024, metadata={"minimum": 64})
    max_pids: int = field(default=128, metadata={"minimum": 8})
    timeout_seconds: float = field(default=3600, metadata={"exclusiveMinimum": 0})
    setup_timeout_seconds: float = field(default=120, metadata={"exclusiveMinimum": 0})
    shutdown_grace_seconds: float = field(default=10, metadata={"exclusiveMinimum": 0})

    def _validate(self):
        require(Path(self.python).is_absolute(), "Harbor worker Python must be an absolute path")
        image_pin(self.image_digest)
        require(re.fullmatch(r"[1-9][0-9]*:[0-9]+", self.user) is not None,
                "Harbor worker requires an explicit non-root uid:gid")
        require(all(arg and "\0" not in arg for arg in self.command), "invalid container command")
        require(PurePosixPath(self.command[0]).is_absolute() and self.command[0] in self.command_files,
                "container executable requires an absolute path and content pin")
        require(all(PurePosixPath(path).is_absolute() and '..' not in PurePosixPath(path).parts
                    and re.fullmatch(r"sha256:[0-9a-f]{64}", digest) for path, digest in self.command_files.items()),
                "invalid container command file pin")
        require(len(set(self.credential_env)) == len(self.credential_env) and all(
            re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) for name in self.credential_env),
            "invalid worker credential environment")
        require(not set(self.credential_env) & {"PYTHONPATH", "PROTEUS_EVALUATION_URL", "PROTEUS_EVALUATION_TOKEN"},
                "credentials cannot override worker protocol configuration")


def validate_harbor_workers(recipe, options):
    require(os.name != 'nt', 'Harbor workers require POSIX supervision')
    for worker in recipe.workers:
        if worker.runtime.plugin.name != 'proteus.worker.harbor':
            continue
        current = HarborWorkerOptions.from_dict(worker.runtime.options)
        if worker.gateway is not None:
            gateway = next(item for item in recipe.gateways if item.id == worker.gateway)
            require(current.network == 'public' and gateway.bind_host == '0.0.0.0',
                    'container gateway access requires public networking and an explicit 0.0.0.0 gateway bind')


class HarborWorkerRuntime(CommandWorkerRuntime):
    def command_options(self, launch, directory):
        options = HarborWorkerOptions.from_dict(launch.options)
        script = Path(__file__).with_name('harbor_bridge.py')
        helper = Path(__file__).parents[1]/'execution/harbor_worker.py'
        bootstrap = Path(__file__).with_name('container_worker.py')
        codex = Path(__file__).with_name('codex_bridge.py')
        request = {'request_hash': content_hash(launch), 'options': options.to_dict(),
                   'workspace': launch.workspace, 'trial_name': 'proteus-'+content_hash(launch).split(':')[1][:32],
                   'bootstrap': str(bootstrap), 'codex_bridge': str(codex)}
        immutable(directory/'harbor-request.json', request)
        paths = (Path(options.python), script, helper, bootstrap, codex)
        return CommandWorkerOptions(command=(options.python, '-I', str(script), 'run',
                                             str(directory/'harbor-request.json')),
            command_files={str(path): 'sha256:'+hashlib.sha256(path.read_bytes()).hexdigest() for path in paths},
            credential_env=options.credential_env, timeout_seconds=options.timeout_seconds,
            shutdown_grace_seconds=options.shutdown_grace_seconds+65)

    def poll(self, launch, directory):
        observed = super().poll(launch, directory)
        if observed.completion is None or not (directory/'harbor-dispatched.json').exists():
            return observed
        cleanup_path = directory/'harbor-cleanup.json'
        if not cleanup_path.exists():
            return WorkerObservation(ref=launch.ref(), state='uncertain')
        cleanup = read_json(cleanup_path.read_text())
        request = read_json((directory/'harbor-request.json').read_text())
        require(request['request_hash'] == content_hash(launch)
                and cleanup['request_hash'] == content_hash(request), 'Harbor cleanup belongs to another worker')
        if not cleanup['cleanup']['verified']:
            return WorkerObservation(ref=launch.ref(), state='uncertain')
        if observed.state in ('completed', 'paused', 'stopped'):
            proof = read_json((directory/'harbor-loaded.json').read_text())
            require(proof['request_hash'] == content_hash(request), 'Harbor container evidence belongs to another worker')
        return observed
