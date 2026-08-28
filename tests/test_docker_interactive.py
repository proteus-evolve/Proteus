from __future__ import annotations

import queue
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from proteus.sandbox import DockerSandbox, SandboxConfig

_EOF = object()


class _RecordingInput:
    def __init__(self) -> None:
        self.data = bytearray()
        self.closed = False
        self.flush_calls = 0
        self.close_calls = 0
        self.flush_error: BaseException | None = None
        self.close_error: BaseException | None = None

    def write(self, data: bytes) -> int:
        if self.closed:
            raise ValueError("write to closed input")
        self.data.extend(data)
        return len(data)

    def flush(self) -> None:
        if self.closed:
            raise ValueError("flush of closed input")
        self.flush_calls += 1
        if self.flush_error is not None:
            raise self.flush_error

    def close(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error
        self.closed = True


class _RecordingOutput:
    def __init__(self) -> None:
        self._items: queue.Queue[bytes | BaseException | object] = queue.Queue()
        self.read_sizes: list[int] = []
        self.return_gate: threading.Event | None = None
        self.read_entered = threading.Event()
        self.next_read_started = threading.Event()
        self.failure_delivered = threading.Event()
        self.reader_thread: threading.Thread | None = None

    def feed(self, data: bytes) -> None:
        self._items.put(data)

    def fail(self, error: BaseException) -> None:
        self._items.put(error)

    def close(self) -> None:
        self._items.put(_EOF)

    def read1(self, size: int) -> bytes:
        self.read_sizes.append(size)
        self.reader_thread = threading.current_thread()
        if len(self.read_sizes) > 1:
            self.next_read_started.set()
        item = self._items.get()
        self.read_entered.set()
        if self.return_gate is not None:
            self.return_gate.wait()
        if item is _EOF:
            return b""
        if isinstance(item, BaseException):
            self.failure_delivered.set()
            raise item
        assert isinstance(item, bytes)
        return item


class _RecordingProcess:
    def __init__(self, argv: list[str], **kwargs: object) -> None:
        self.args = argv
        self.returncode: int | None = None
        self.stdin = _RecordingInput()
        self.stdout = _RecordingOutput()
        self.stderr = _RecordingOutput()
        self.wait_calls = 0
        self.terminate_calls = 0
        self.kill_calls = 0
        self.ignore_terminate = False
        self.on_terminate = None
        self._complete = threading.Event()

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls += 1
        if not self._complete.wait(timeout):
            raise subprocess.TimeoutExpired(self.args, timeout)
        assert self.returncode is not None
        return self.returncode

    def terminate(self) -> None:
        self.terminate_calls += 1
        if self.on_terminate is not None:
            self.on_terminate()
        if not self.ignore_terminate:
            self.complete(-15)

    def kill(self) -> None:
        self.kill_calls += 1
        self.complete(-9)

    def complete(self, returncode: int = 0) -> None:
        if self._complete.is_set():
            return
        self.returncode = returncode
        self.stdout.close()
        self.stderr.close()
        self._complete.set()


class _RecordedDocker:
    def __init__(self) -> None:
        self.popen_calls: list[tuple[list[str], dict[str, object]]] = []
        self.run_calls: list[list[str]] = []
        self.processes: list[_RecordingProcess] = []
        self.containers: set[str] = set()
        self.rm_returncodes: list[int] = []
        self.complete_client_on_remove = True
        self.delay_registration_until_terminate = False
        self.events: list[str] = []

    @property
    def single_argv(self) -> list[str]:
        assert len(self.popen_calls) == 1
        return self.popen_calls[0][0]


@contextmanager
def _record_docker(monkeypatch: pytest.MonkeyPatch) -> Iterator[_RecordedDocker]:
    recorded = _RecordedDocker()

    def popen(argv: list[str], **kwargs: object) -> _RecordingProcess:
        process = _RecordingProcess(argv, **kwargs)
        recorded.popen_calls.append((argv, kwargs))
        recorded.processes.append(process)
        name = argv[argv.index("--name") + 1]
        if recorded.delay_registration_until_terminate:
            def register_late() -> None:
                recorded.events.append("client_terminated")
                recorded.containers.add(name)

            process.on_terminate = register_late
        else:
            recorded.containers.add(name)
        return process

    def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        recorded.run_calls.append(argv)
        if argv[:3] == ["docker", "rm", "-f"]:
            recorded.events.append("rm")
            returncode = (
                recorded.rm_returncodes.pop(0)
                if recorded.rm_returncodes
                else 0
            )
            if returncode == 0:
                recorded.containers.discard(argv[3])
                if recorded.complete_client_on_remove:
                    for process in recorded.processes:
                        process.complete(137)
            return subprocess.CompletedProcess(argv, returncode, b"", b"")
        if argv[:3] == ["docker", "container", "inspect"]:
            recorded.events.append("inspect")
            returncode = 0 if argv[3] in recorded.containers else 1
            return subprocess.CompletedProcess(argv, returncode, b"", b"")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(subprocess, "run", run)
    yield recorded


def test_interactive_docker_uses_network_none_absolute_mounts_and_key_names_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = DockerSandbox(
        SandboxConfig(
            image="proteus-env-aki-src:0.1.0",
            network="none",
            env_passthrough=("OPENAI_API_KEY",),
        )
    )
    monkeypatch.chdir(tmp_path)

    with _record_docker(monkeypatch) as recorded:
        session = sandbox.open_session(
            Path("runs/session"),
            ["episode"],
            env={"OPENAI_API_KEY": "never-in-argv"},
            mounts=(("candidate", "/workspace/candidate"),),
        )
        session.abort()

    argv = recorded.single_argv
    popen_kwargs = recorded.popen_calls[0][1]
    assert argv[argv.index("--network") + 1] == "none"
    assert "never-in-argv" not in argv
    assert "OPENAI_API_KEY" in argv
    assert str((tmp_path / "candidate").resolve()) in " ".join(argv)
    assert popen_kwargs["stdin"] is subprocess.PIPE
    assert popen_kwargs["stdout"] is subprocess.PIPE
    assert popen_kwargs["stderr"] is subprocess.PIPE
    assert "text" not in popen_kwargs
    assert popen_kwargs["env"]["OPENAI_API_KEY"] == "never-in-argv"  # type: ignore[index]


def test_interactive_docker_writes_and_reads_exact_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _record_docker(monkeypatch) as recorded:
        session = DockerSandbox(SandboxConfig(image="image")).open_session(
            tmp_path, ["episode"], env={}
        )
        process = recorded.processes[0]
        process.stdout.feed(b"abc")
        process.stdout.feed(b"def")

        session.write(b"request")
        assert session.read_exact(4, timeout_s=1) == b"abcd"
        assert session.read_exact(2, timeout_s=1) == b"ef"
        session.abort()

    assert bytes(process.stdin.data) == b"request"
    assert process.stdin.flush_calls == 1
    assert process.stdout.read_sizes and all(size > 0 for size in process.stdout.read_sizes)


def test_interactive_docker_timeout_removes_named_container_before_raising(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _record_docker(monkeypatch) as recorded:
        session = DockerSandbox(SandboxConfig(image="image")).open_session(
            tmp_path, ["episode"], env={}
        )
        process = recorded.processes[0]

        with pytest.raises(subprocess.TimeoutExpired):
            session.finish(timeout_s=0)

    assert process.stdin.closed
    assert ["docker", "rm", "-f", session.container_name] in recorded.run_calls
    assert process.wait_calls >= 2


def test_read_timeout_removes_named_container_before_raising(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _record_docker(monkeypatch) as recorded:
        session = DockerSandbox(SandboxConfig(image="image")).open_session(
            tmp_path, ["episode"], env={}
        )
        process = recorded.processes[0]

        with pytest.raises(subprocess.TimeoutExpired):
            session.read_exact(1, timeout_s=0)

    assert process.stdin.closed
    assert ["docker", "rm", "-f", session.container_name] in recorded.run_calls
    assert process.wait_calls == 1


def test_protocol_reader_failure_cleans_up_before_raising(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _record_docker(monkeypatch) as recorded:
        session = DockerSandbox(SandboxConfig(image="image")).open_session(
            tmp_path, ["episode"], env={}
        )
        process = recorded.processes[0]
        process.stderr.feed(b"diagnostic")
        failure = OSError("protocol reader failed")
        process.stdout.fail(failure)

        with pytest.raises(OSError, match="protocol reader failed"):
            session.read_exact(1, timeout_s=1)

        cleaned_before_raise = (
            process.stdin.closed,
            ["docker", "rm", "-f", session.container_name] in recorded.run_calls,
            process.wait_calls,
        )
        result = session.abort()

    assert cleaned_before_raise == (True, True, 1)
    assert result.stderr == b"diagnostic"
    assert process.stderr._items.empty()


def test_finish_waits_for_stdout_and_stderr_readers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _record_docker(monkeypatch) as recorded:
        session = DockerSandbox(SandboxConfig(image="image")).open_session(
            tmp_path, ["episode"], env={}
        )
        process = recorded.processes[0]
        gate = threading.Event()
        process.stdout.return_gate = gate
        process.stdout.feed(b"late-output")
        assert process.stdout.read_entered.wait(1)
        process.complete()

        finished = threading.Event()
        results: list[subprocess.CompletedProcess[bytes]] = []

        def finish() -> None:
            results.append(session.finish(timeout_s=1))
            finished.set()

        finisher = threading.Thread(target=finish)
        finisher.start()
        returned_while_reader_blocked = finished.wait(0.05)
        gate.set()
        finisher.join(1)

    assert not returned_while_reader_blocked
    assert not finisher.is_alive()
    assert results[0].stdout == b"late-output"


def test_finish_waits_for_stderr_reader_before_publishing_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _record_docker(monkeypatch) as recorded:
        session = DockerSandbox(SandboxConfig(image="image")).open_session(
            tmp_path, ["episode"], env={}
        )
        process = recorded.processes[0]
        gate = threading.Event()
        process.stderr.return_gate = gate
        process.stderr.feed(b"late-diagnostic")
        assert process.stderr.read_entered.wait(1)
        process.complete()

        finished = threading.Event()
        results: list[subprocess.CompletedProcess[bytes]] = []

        def finish() -> None:
            results.append(session.finish(timeout_s=1))
            finished.set()

        finisher = threading.Thread(target=finish)
        finisher.start()
        returned_while_reader_blocked = finished.wait(0.05)
        gate.set()
        finisher.join(1)

    assert not returned_while_reader_blocked
    assert not finisher.is_alive()
    assert results[0].stderr == b"late-diagnostic"


def test_abort_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _record_docker(monkeypatch) as recorded:
        session = DockerSandbox(SandboxConfig(image="image")).open_session(
            tmp_path, ["episode"], env={}
        )

        first = session.abort()
        second = session.abort()

    remove = ["docker", "rm", "-f", session.container_name]
    assert recorded.run_calls.count(remove) == 1
    assert first is second


def test_finish_confirms_container_absence_before_idempotent_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutation caught: caching a clean client exit while its container still exists."""
    with _record_docker(monkeypatch) as recorded:
        session = DockerSandbox(SandboxConfig(image="image")).open_session(
            tmp_path, ["episode"], env={}
        )
        process = recorded.processes[0]
        process.complete(0)

        first = session.finish(timeout_s=1)
        second = session.abort()

    remove = ["docker", "rm", "-f", session.container_name]
    assert recorded.run_calls.count(remove) == 1
    assert session.container_name not in recorded.containers
    assert first is second


def test_relative_default_and_explicit_mounts_are_absolute(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    sandbox = DockerSandbox(
        SandboxConfig(image="image", extra_mounts=(("shared", "/shared"),))
    )
    with _record_docker(monkeypatch) as recorded:
        default_session = sandbox.open_session(Path("runs/default"), ["episode"], env={})
        default_session.abort()
        explicit_session = sandbox.open_session(
            Path("runs/unused"),
            ["episode"],
            env={},
            mounts=(("candidate", "/workspace/candidate", "ro"),),
        )
        explicit_session.abort()

    default_volumes = [
        argv[index + 1]
        for argv, _kwargs in recorded.popen_calls[:1]
        for index, value in enumerate(argv)
        if value == "-v"
    ]
    explicit_volumes = [
        argv[index + 1]
        for argv, _kwargs in recorded.popen_calls[1:]
        for index, value in enumerate(argv)
        if value == "-v"
    ]
    assert default_volumes == [
        f"{(tmp_path / 'runs/default').resolve()}:/run",
        f"{(tmp_path / 'shared').resolve()}:/shared",
    ]
    assert explicit_volumes == [
        f"{(tmp_path / 'candidate').resolve()}:/workspace/candidate:ro",
        f"{(tmp_path / 'shared').resolve()}:/shared",
    ]


def test_secret_value_is_absent_from_completed_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "controller-only-secret"
    with _record_docker(monkeypatch) as recorded:
        session = DockerSandbox(
            SandboxConfig(image="image", env_passthrough=("OPENAI_API_KEY",))
        ).open_session(tmp_path, ["episode"], env={"OPENAI_API_KEY": secret})
        process = recorded.processes[0]
        process.stdout.feed(b"response")
        process.stderr.feed(b"diagnostic")
        process.complete(7)

        result = session.finish(timeout_s=1)

    assert secret not in " ".join(result.args)
    assert result.args == tuple(recorded.single_argv)
    assert result.stdout == b"response"
    assert result.stderr == b"diagnostic"
    assert isinstance(result.stdout, bytes)
    assert isinstance(result.stderr, bytes)


@pytest.mark.parametrize("operation", ["write", "finish", "abort"])
def test_broken_stdin_does_not_stop_cleanup_or_mask_initiating_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    with _record_docker(monkeypatch) as recorded:
        session = DockerSandbox(SandboxConfig(image="image")).open_session(
            tmp_path, ["episode"], env={}
        )
        process = recorded.processes[0]
        process.stdout.feed(b"partial-output")
        process.stderr.feed(b"diagnostic")
        if operation == "write":
            process.stdin.flush_error = BrokenPipeError("flush failed")
            process.stdin.close_error = BrokenPipeError("close failed")
            expected = "flush failed"
        else:
            process.stdin.close_error = BrokenPipeError("close failed")
            expected = "close failed"

        try:
            with pytest.raises(BrokenPipeError, match=expected):
                if operation == "write":
                    session.write(b"request")
                elif operation == "finish":
                    session.finish(timeout_s=1)
                else:
                    session.abort()
        finally:
            # Prevent a failed current implementation from leaving its process-double
            # readers blocked after the RED assertion captures the missing cleanup.
            process.complete(137)

    assert ["docker", "rm", "-f", session.container_name] in recorded.run_calls
    assert process.wait_calls >= 1
    assert process.stdout._items.empty()
    assert process.stderr._items.empty()


def test_buffered_frame_does_not_bypass_other_reader_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _record_docker(monkeypatch) as recorded:
        session = DockerSandbox(SandboxConfig(image="image")).open_session(
            tmp_path, ["episode"], env={}
        )
        process = recorded.processes[0]
        process.stdout.feed(b"frame")
        assert process.stdout.next_read_started.wait(1)
        process.stderr.fail(OSError("stderr reader failed"))
        assert process.stderr.failure_delivered.wait(1)
        assert process.stderr.reader_thread is not None
        process.stderr.reader_thread.join(1)
        assert not process.stderr.reader_thread.is_alive()

        try:
            with pytest.raises(OSError, match="stderr reader failed"):
                session.read_exact(5, timeout_s=1)
        finally:
            session.abort()

    assert ["docker", "rm", "-f", session.container_name] in recorded.run_calls
    assert process.wait_calls >= 1


def test_failed_removal_is_reported_and_retried_until_absence_is_verified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutation caught: marking removal before Docker confirms container absence."""
    with _record_docker(monkeypatch) as recorded:
        session = DockerSandbox(SandboxConfig(image="image")).open_session(
            tmp_path, ["episode"], env={}
        )
        recorded.rm_returncodes[:] = [1, 1]

        with pytest.raises(RuntimeError, match="container.*still exists"):
            session.abort()

        assert session.container_name in recorded.containers
        recorded.rm_returncodes[:] = [0]
        result = session.abort()

    remove = ["docker", "rm", "-f", session.container_name]
    assert recorded.run_calls.count(remove) == 3
    assert session.container_name not in recorded.containers
    assert result.returncode in {-15, -9, 137}


def test_abort_reaps_client_before_confirming_delayed_container_absence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A late Docker registration cannot occur after cleanup publishes success."""
    with _record_docker(monkeypatch) as recorded:
        recorded.delay_registration_until_terminate = True
        recorded.complete_client_on_remove = False
        session = DockerSandbox(SandboxConfig(image="image")).open_session(
            tmp_path, ["episode"], env={}
        )
        process = recorded.processes[0]

        first = session.abort()
        second = session.abort()

    remove = ["docker", "rm", "-f", session.container_name]
    assert recorded.events[:3] == ["client_terminated", "rm", "inspect"]
    assert process.poll() is not None
    assert session.container_name not in recorded.containers
    assert recorded.run_calls.count(remove) == 1
    assert first is second


def test_abort_bounds_stuck_docker_client_wait(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutation caught: an unbounded Docker-client wait after successful removal."""
    from proteus.sandbox import docker_session

    monkeypatch.setattr(docker_session, "_CLEANUP_TIMEOUT_S", 0.05)
    with _record_docker(monkeypatch) as recorded:
        recorded.complete_client_on_remove = False
        session = DockerSandbox(SandboxConfig(image="image")).open_session(
            tmp_path, ["episode"], env={}
        )
        process = recorded.processes[0]
        process.ignore_terminate = True
        release = threading.Timer(0.25, process.complete, args=(-9,))
        release.start()
        started = time.monotonic()
        try:
            session.abort()
        finally:
            elapsed = time.monotonic() - started
            release.cancel()
            process.complete(-9)

    assert elapsed < 0.2
    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert session.container_name not in recorded.containers


def test_abort_bounds_and_reports_blocked_pipe_reader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutation caught: unbounded reader joins during abort cleanup."""
    from proteus.sandbox import docker_session

    monkeypatch.setattr(docker_session, "_CLEANUP_TIMEOUT_S", 0.05)
    with _record_docker(monkeypatch) as recorded:
        session = DockerSandbox(SandboxConfig(image="image")).open_session(
            tmp_path, ["episode"], env={}
        )
        process = recorded.processes[0]
        gate = threading.Event()
        process.stdout.return_gate = gate
        process.stdout.feed(b"blocked-output")
        assert process.stdout.read_entered.wait(1)
        release = threading.Timer(0.25, gate.set)
        release.start()
        started = time.monotonic()
        try:
            with pytest.raises(RuntimeError, match="reader.*did not stop"):
                session.abort()
        finally:
            elapsed = time.monotonic() - started
            gate.set()
            release.cancel()
            assert process.stdout.reader_thread is not None
            process.stdout.reader_thread.join(1)

        result = session.abort()

    remove = ["docker", "rm", "-f", session.container_name]
    assert elapsed < 0.2
    assert recorded.run_calls.count(remove) == 1
    assert result.stdout == b"blocked-output"


def test_cold_smoke_commits_direct_runtime_image_and_removes_container(tmp_path: Path) -> None:
    """A warmed snapshot can be reused without preserving its disposable container."""
    import proteus.sandbox.docker as mod

    calls = []

    def fake_run(argv, **_kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    real, mod.subprocess.run = mod.subprocess.run, fake_run
    try:
        completed = DockerSandbox(SandboxConfig(image="base-image")).run_and_commit_image(
            tmp_path,
            ["--proteus-headless-smoke"],
            {},
            timeout_s=1,
            runtime_image="proteus-dsh-runtime:example",
            entrypoint=("node", "/opt/src/apps/cli/lib/bin.js"),
            mounts=((str(tmp_path / "workspace"), "/workspace"),),
        )
    finally:
        mod.subprocess.run = real

    smoke, commit, remove = calls
    name = smoke[smoke.index("--name") + 1]
    assert completed.returncode == 0
    assert "--rm" not in smoke
    assert smoke[smoke.index("base-image") + 1:] == ["--proteus-headless-smoke"]
    assert commit == [
        "docker",
        "commit",
        "--change",
        'ENTRYPOINT ["node","/opt/src/apps/cli/lib/bin.js"]',
        name,
        "proteus-dsh-runtime:example",
    ]
    assert remove == ["docker", "rm", "-f", name]


def test_cold_smoke_failure_does_not_publish_runtime_and_removes_container(tmp_path: Path) -> None:
    import proteus.sandbox.docker as mod

    calls = []

    def fake_run(argv, **_kwargs):
        calls.append(argv)
        if argv[:2] == ["docker", "run"]:
            return subprocess.CompletedProcess(argv, 91, "", "cold failure")
        return subprocess.CompletedProcess(argv, 0, "", "")

    real, mod.subprocess.run = mod.subprocess.run, fake_run
    try:
        completed = DockerSandbox(SandboxConfig(image="base-image")).run_and_commit_image(
            tmp_path,
            ["--proteus-headless-smoke"],
            {},
            timeout_s=1,
            runtime_image="proteus-dsh-runtime:example",
            entrypoint=("node", "/opt/src/apps/cli/lib/bin.js"),
        )
    finally:
        mod.subprocess.run = real

    smoke, remove = calls
    name = smoke[smoke.index("--name") + 1]
    assert completed.returncode == 91
    assert not any(call[:2] == ["docker", "commit"] for call in calls)
    assert remove == ["docker", "rm", "-f", name]


def test_failed_runtime_commit_is_reported_and_removes_container(tmp_path: Path) -> None:
    import proteus.sandbox.docker as mod

    calls = []

    def fake_run(argv, **_kwargs):
        calls.append(argv)
        if argv[:2] == ["docker", "commit"]:
            return subprocess.CompletedProcess(argv, 42, "", "tag rejected")
        return subprocess.CompletedProcess(argv, 0, "smoked", "")

    real, mod.subprocess.run = mod.subprocess.run, fake_run
    try:
        completed = DockerSandbox(SandboxConfig(image="base-image")).run_and_commit_image(
            tmp_path,
            ["--proteus-headless-smoke"],
            {},
            timeout_s=1,
            runtime_image="proteus-dsh-runtime:example",
            entrypoint=("node", "/opt/src/apps/cli/lib/bin.js"),
        )
    finally:
        mod.subprocess.run = real

    smoke, commit, remove = calls
    name = smoke[smoke.index("--name") + 1]
    assert completed.returncode == 42
    assert "failed to publish committed runtime image: tag rejected" in completed.stderr
    assert commit[:2] == ["docker", "commit"]
    assert remove == ["docker", "rm", "-f", name]


def test_cold_smoke_timeout_removes_uncommitted_container(tmp_path: Path) -> None:
    import subprocess as sp

    import proteus.sandbox.docker as mod

    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[:2] == ["docker", "run"]:
            raise sp.TimeoutExpired(argv, kwargs["timeout"])
        return sp.CompletedProcess(argv, 0, "", "")

    real, mod.subprocess.run = mod.subprocess.run, fake_run
    try:
        with pytest.raises(sp.TimeoutExpired):
            DockerSandbox(SandboxConfig(image="base-image")).run_and_commit_image(
                tmp_path,
                ["--proteus-headless-smoke"],
                {},
                timeout_s=1,
                runtime_image="proteus-dsh-runtime:example",
                entrypoint=("node", "/opt/src/apps/cli/lib/bin.js"),
            )
    finally:
        mod.subprocess.run = real

    name = calls[0][calls[0].index("--name") + 1]
    assert calls[1] == ["docker", "rm", "-f", name]


def test_image_id_returns_immutable_id_or_empty_for_missing_image() -> None:
    import proteus.sandbox.docker as mod

    calls = []

    def fake_run(argv, **_kwargs):
        calls.append(argv)
        if argv[-1] == "present":
            return subprocess.CompletedProcess(argv, 0, "sha256:immutable\n", "")
        return subprocess.CompletedProcess(argv, 1, "", "not found")

    real, mod.subprocess.run = mod.subprocess.run, fake_run
    try:
        assert DockerSandbox.image_id("present") == "sha256:immutable"
        assert DockerSandbox.image_id("missing") == ""
        assert DockerSandbox.image_id("") == ""
    finally:
        mod.subprocess.run = real

    assert calls == [
        ["docker", "image", "inspect", "--format", "{{.Id}}", "present"],
        ["docker", "image", "inspect", "--format", "{{.Id}}", "missing"],
    ]


@pytest.mark.parametrize(
    ("current_id", "remove_returncode", "expected", "removed"),
    (
        ("", 0, True, False),
        ("sha256:other", 0, False, False),
        ("sha256:owned", 1, False, True),
        ("sha256:owned", 0, True, True),
    ),
)
def test_remove_image_requires_exact_identity_and_reports_removal(
    monkeypatch: pytest.MonkeyPatch,
    current_id: str,
    remove_returncode: int,
    expected: bool,
    removed: bool,
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(DockerSandbox, "image_id", lambda _image: current_id)

    def fake_run(argv, **_kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, remove_returncode, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert DockerSandbox.remove_image(
        "proteus-dsh-runtime:owned",
        expected_image_id="sha256:owned",
    ) is expected
    assert bool(calls) is removed
    if removed:
        assert calls == [["docker", "image", "rm", "proteus-dsh-runtime:owned"]]
