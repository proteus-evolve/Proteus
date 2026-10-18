"""Trusted command bridge and OS leases; no signaling a stored/reused PID."""

from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import sys
from typing import Protocol

from proteus.artifacts.store import _sync_directory
from proteus.core.contracts import require
from proteus.core.serialization import canonical_json, content_hash, read_json
from proteus.workers.records import CommandWorkerOptions, WorkerCompletion, WorkerObservation


@contextmanager
def lease(path, *, blocking=True):
    import fcntl
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a+") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield None
        else:
            # Closing, rather than explicit LOCK_UN, preserves a lease inherited
            # by a supervisor across a controller exit.
            yield stream


def write_json(path, value):
    import tempfile
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(canonical_json(value) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def immutable(path, value):
    if path.exists():
        require(read_json(path.read_text()) == read_json(canonical_json(value)), "worker operation identity changed")
    else:
        write_json(path, value)


class WorkerRuntime(Protocol):
    def reservation(self, options): ...
    def start(self, launch, directory: Path) -> None: ...
    def poll(self, launch, directory: Path) -> WorkerObservation: ...
    def control(self, launch, directory: Path, action: str) -> None: ...


class CommandWorkerRuntime:
    """POSIX local backend for trusted bridges; this is not an OS sandbox.

    Provider adapters and isolated backends implement the same lifecycle. The
    command protocol cannot enforce a model-call or monetary cap; unknown usage
    remains unknown. Runtime options are included in every launch identity.
    """

    def reservation(self, options):
        from proteus.core.ledger import Amounts
        return Amounts()

    def start(self, launch, directory):
        options = CommandWorkerOptions.from_dict(launch.options)
        with lease(directory / "supervisor.lock", blocking=False) as ownership:
            if ownership is None or (directory / "dispatch.json").exists():
                return  # already running, completed or uncertain; never resubmit
            import hashlib
            require(all("sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest
                        for path, digest in options.command_files.items()), "worker command file changed")
            require(all(name in os.environ for name in options.credential_env),
                    "missing worker credential environment variable")
            environment = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "TMPDIR") if key in os.environ}
            environment.update({key: os.environ[key] for key in options.credential_env})
            # The supervisor is a standalone stdlib script. Installed packages
            # work outside the source checkout without PYTHONPATH injection.
            with (directory / "supervisor.log").open("ab") as log:
                child = subprocess.Popen([sys.executable, str(Path(__file__).with_name("supervisor.py")),
                    str(directory / "request.json"), str(ownership.fileno())],
                    stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=environment,
                    pass_fds=(ownership.fileno(),), start_new_session=True)
            # Popen's object is not a durable identity. Reaping is best effort;
            # ownership and completion are proven by the lease and receipt.
            import threading
            threading.Thread(target=child.wait, daemon=True).start()

    def poll(self, launch, directory):
        ref = launch.ref()
        receipt = directory / "completion.json"
        if receipt.exists():
            completion = WorkerCompletion.from_json(receipt.read_text())
            require(completion.request_hash == content_hash(launch), "worker completion request differs")
            import hashlib
            for name in ("stdout", "stderr"):
                digest = "sha256:" + hashlib.sha256((directory / (name + ".log")).read_bytes()).hexdigest()
                require(digest == getattr(completion, name + "_hash"), "worker output changed after completion")
            return WorkerObservation(ref=ref, state=completion.status, completion=completion)
        with lease(directory / "supervisor.lock", blocking=False) as ownership:
            active = ownership is None
        if active:
            state = "active"
        else:
            # Recheck after obtaining the lease: completion can be committed
            # between the first read and the supervisor releasing ownership.
            if receipt.exists():
                return self.poll(launch, directory)
            state = "uncertain" if (directory / "dispatch.json").exists() else "pending"
        return WorkerObservation(ref=ref, state=state)

    def control(self, launch, directory, action):
        require(action in ("pause", "stop"), "unknown worker control action")
        with lease(directory / "control.lock"):
            path = directory / "control.json"
            if path.exists():
                prior = read_json(path.read_text())
                require(prior["request_hash"] == content_hash(launch), "worker control identity differs")
                if prior["action"] == "stop" or prior["action"] == action:
                    return
            write_json(path, {"request_hash": content_hash(launch), "action": action})
