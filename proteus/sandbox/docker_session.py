from __future__ import annotations

import subprocess
import threading
import time
from typing import BinaryIO


_READ_SIZE = 64 * 1024
_CLEANUP_TIMEOUT_S = 5.0


def _annotate_cleanup_failure(primary: BaseException, cleanup: BaseException) -> None:
    context = f"interactive Docker cleanup failed: {cleanup}"
    setattr(primary, "cleanup_context", context)
    primary.__cause__ = cleanup
    add_note = getattr(primary, "add_note", None)
    if callable(add_note):
        add_note(context)


class DockerInteractiveSession:
    """Own one interactive Docker client and its named container."""

    def __init__(
        self,
        process: subprocess.Popen[bytes],
        *,
        container_name: str,
        argv: list[str],
    ) -> None:
        self._process = process
        self._container_name = container_name
        self._argv = tuple(argv)
        self._condition = threading.Condition()
        self._stdout_buffer = bytearray()
        self._stdout_chunks: list[bytes] = []
        self._stderr_chunks: list[bytes] = []
        self._reader_errors: list[BaseException] = []
        self._stdout_eof = False
        self._stderr_eof = False
        self._result: subprocess.CompletedProcess[bytes] | None = None
        self._removed = False
        self._finish_lock = threading.Lock()
        assert process.stdout is not None
        assert process.stderr is not None
        self._readers = (
            threading.Thread(
                target=self._read_pipe,
                args=(process.stdout, True),
                name=f"{container_name}-stdout",
                daemon=True,
            ),
            threading.Thread(
                target=self._read_pipe,
                args=(process.stderr, False),
                name=f"{container_name}-stderr",
                daemon=True,
            ),
        )
        for reader in self._readers:
            reader.start()

    @property
    def container_name(self) -> str:
        return self._container_name

    def _read_pipe(self, pipe: BinaryIO, stdout: bool) -> None:
        try:
            read = getattr(pipe, "read1", None) or pipe.read
            while chunk := read(_READ_SIZE):
                immutable = bytes(chunk)
                with self._condition:
                    if stdout:
                        self._stdout_chunks.append(immutable)
                        self._stdout_buffer.extend(immutable)
                    else:
                        self._stderr_chunks.append(immutable)
                    self._condition.notify_all()
        except BaseException as exc:
            with self._condition:
                self._reader_errors.append(exc)
                self._condition.notify_all()
        finally:
            with self._condition:
                if stdout:
                    self._stdout_eof = True
                else:
                    self._stderr_eof = True
                self._condition.notify_all()

    def write(self, data: bytes) -> None:
        try:
            if self._process.stdin is None or self._process.stdin.closed:
                raise BrokenPipeError("interactive Docker input is closed")
            self._process.stdin.write(data)
            self._process.stdin.flush()
        except BaseException as primary:
            _result, cleanup = self._abort_result()
            if cleanup is not None:
                _annotate_cleanup_failure(primary, cleanup)
            raise

    def read_exact(self, size: int, *, timeout_s: float) -> bytes:
        if size < 0:
            raise ValueError("size must be non-negative")
        deadline = time.monotonic() + timeout_s
        failure: BaseException | None = None
        with self._condition:
            while True:
                if self._reader_errors:
                    failure = self._reader_errors[0]
                    break
                if len(self._stdout_buffer) >= size:
                    data = bytes(self._stdout_buffer[:size])
                    del self._stdout_buffer[:size]
                    return data
                if self._stdout_eof:
                    failure = EOFError(
                        f"interactive Docker output ended before {size} bytes were available"
                    )
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)

        if failure is not None:
            _result, cleanup = self._abort_result()
            if cleanup is not None:
                _annotate_cleanup_failure(failure, cleanup)
            raise failure
        result, cleanup = self._abort_result()
        timeout = subprocess.TimeoutExpired(
            self._argv,
            timeout_s,
            output=result.stdout,
            stderr=result.stderr,
        )
        if cleanup is not None:
            _annotate_cleanup_failure(timeout, cleanup)
        raise timeout

    def close_input(self) -> None:
        if self._process.stdin is not None and not self._process.stdin.closed:
            self._process.stdin.close()

    @staticmethod
    def _diagnostic_text(result: subprocess.CompletedProcess[bytes]) -> str:
        return b"\n".join((result.stdout or b"", result.stderr or b"")).decode(
            "utf-8", errors="replace"
        )

    def _run_cleanup(self, argv: list[str]) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            argv,
            capture_output=True,
            check=False,
            timeout=_CLEANUP_TIMEOUT_S,
        )

    def _container_is_present(
        self, *, removal: subprocess.CompletedProcess[bytes] | None
    ) -> bool:
        inspected = self._run_cleanup(
            ["docker", "container", "inspect", self._container_name]
        )
        if inspected.returncode == 0:
            return True
        diagnostics = self._diagnostic_text(inspected).lower()
        if removal is not None and removal.returncode == 0:
            return False
        if "no such" in diagnostics or "not found" in diagnostics:
            return False
        raise RuntimeError(
            f"could not verify removal of interactive Docker container "
            f"{self._container_name!r}"
        )

    def _stop_client(self) -> int:
        returncode = self._process.poll()
        if returncode is not None:
            return returncode
        self._process.terminate()
        try:
            return self._process.wait(timeout=_CLEANUP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            pass
        self._process.kill()
        return self._process.wait(timeout=_CLEANUP_TIMEOUT_S)

    def _remove_container(self, *, client_reaped: bool = True) -> None:
        if self._removed:
            return
        diagnostics: list[str] = []
        for _ in range(2):
            removal: subprocess.CompletedProcess[bytes] | None = None
            try:
                removal = self._run_cleanup(
                    ["docker", "rm", "-f", self._container_name]
                )
                if removal.returncode != 0:
                    diagnostics.append(self._diagnostic_text(removal).strip())
            except BaseException as exc:
                diagnostics.append(str(exc))
            try:
                present = self._container_is_present(removal=removal)
            except BaseException as exc:
                diagnostics.append(str(exc))
                present = True
            if not present:
                self._removed = client_reaped
                return
        detail = "; ".join(item for item in diagnostics if item)
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(
            f"interactive Docker container {self._container_name!r} still exists "
            f"after removal{suffix}"
        )

    def _join_readers(self) -> None:
        for reader in self._readers:
            reader.join(_CLEANUP_TIMEOUT_S)
        alive = [reader.name for reader in self._readers if reader.is_alive()]
        if alive:
            raise RuntimeError(
                "interactive Docker reader thread did not stop: " + ", ".join(alive)
            )

    def _completed_process(self, returncode: int) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(
            self._argv,
            returncode,
            b"".join(self._stdout_chunks),
            b"".join(self._stderr_chunks),
        )

    def finish(self, *, timeout_s: float) -> subprocess.CompletedProcess[bytes]:
        if self._result is not None:
            return self._result
        try:
            self.close_input()
        except BaseException as primary:
            _result, cleanup = self._abort_result()
            if cleanup is not None:
                _annotate_cleanup_failure(primary, cleanup)
            raise
        deadline = time.monotonic() + timeout_s
        try:
            returncode = self._process.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            result, cleanup = self._abort_result()
            timeout = subprocess.TimeoutExpired(
                self._argv,
                timeout_s,
                output=result.stdout,
                stderr=result.stderr,
            )
            if cleanup is not None:
                _annotate_cleanup_failure(timeout, cleanup)
            raise timeout from None

        for reader in self._readers:
            reader.join(max(0.0, deadline - time.monotonic()))
        if any(reader.is_alive() for reader in self._readers):
            result, cleanup = self._abort_result()
            timeout = subprocess.TimeoutExpired(
                self._argv,
                timeout_s,
                output=result.stdout,
                stderr=result.stderr,
            )
            if cleanup is not None:
                _annotate_cleanup_failure(timeout, cleanup)
            raise timeout
        if self._reader_errors:
            failure = self._reader_errors[0]
            _result, cleanup = self._abort_result()
            if cleanup is not None:
                _annotate_cleanup_failure(failure, cleanup)
            raise failure
        if self._container_is_present(removal=None):
            self._remove_container()
        else:
            self._removed = True
        with self._finish_lock:
            if self._result is None:
                self._result = self._completed_process(returncode)
            return self._result

    def _abort_result(
        self,
    ) -> tuple[subprocess.CompletedProcess[bytes], BaseException | None]:
        with self._finish_lock:
            if self._result is not None:
                return self._result, None
            failure: BaseException | None = None

            try:
                self.close_input()
            except BaseException as exc:
                failure = exc
            try:
                returncode = self._stop_client()
                client_reaped = True
            except BaseException as exc:
                if failure is None:
                    failure = exc
                returncode = self._process.returncode
                client_reaped = False
            try:
                self._remove_container(client_reaped=client_reaped)
            except BaseException as exc:
                if failure is None:
                    failure = exc
            try:
                self._join_readers()
            except BaseException as exc:
                if failure is None:
                    failure = exc
            if returncode is None:
                returncode = -1
            result = self._completed_process(returncode)
            if failure is None:
                self._result = result
            return result, failure

    def abort(self) -> subprocess.CompletedProcess[bytes]:
        result, failure = self._abort_result()
        if failure is not None:
            raise failure
        return result
