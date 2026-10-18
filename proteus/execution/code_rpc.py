"""Bounded local candidate RPC with host-owned model dispatch and process cleanup."""

from __future__ import annotations

import os
import selectors
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from proteus.core.contracts import OperationStatus, Outcome, require
from proteus.core.serialization import canonical_json, read_json
from proteus.core.recovery import RecoveryRequired
from proteus.evaluation.records import Trace


@dataclass(frozen=True)
class Reply:
    status: Outcome
    output: Any
    state: Any
    trace: Trace
    diagnostic: str


class ProtocolFailure(ValueError):
    pass


class GatewayFailure(RuntimeError):
    pass


def invoke(root, options, call, actor, *, timeout_seconds):
    """Run one method and serialize state; model calls execute on the dispatch thread.

    Candidate CPU/I/O has a wall-clock deadline. Each managed model transport gets
    the remaining timeout, and the deadline is rechecked when it returns. This is
    a trusted host provider; an isolated container provider is a separate backend.
    """
    require(os.name == 'posix' and timeout_seconds is not None and timeout_seconds > 0,
            'candidate RPC requires POSIX and a finite execution timeout')
    require(call.method in options.methods, 'candidate operation is not declared')
    deadline = time.monotonic() + timeout_seconds
    parent, child = socket.socketpair()
    process = None
    outputs = {'stdout': bytearray(), 'stderr': bytearray()}
    buffer = bytearray()
    ordinal, terminal, status, diagnostic = 0, None, Outcome.SUCCEEDED, ''
    events, trace_bytes = [], 0
    selector = selectors.DefaultSelector()

    def remaining():
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise TimeoutError('candidate code invocation timed out')
        return seconds

    def send(value):
        data = canonical_json(value).encode() + b'\n'
        if len(data) > options.max_frame_bytes:
            raise ProtocolFailure('outgoing RPC frame exceeds the declared byte limit')
        parent.setblocking(True)
        parent.settimeout(remaining())
        parent.sendall(data)
        parent.setblocking(False)

    try:
        # No inherited credentials, Python configuration, HOME or writable shared cwd.
        # Python -I still imports installed packages; this is not a host-access sandbox.
        process = subprocess.Popen([sys.executable, '-I', str(Path(__file__).with_name('code_worker.py')),
            str(child.fileno()), str(options.max_frame_bytes), str(os.getpid()), repr(deadline)], cwd=root,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={'PATH': os.defpath},
            pass_fds=(child.fileno(),), start_new_session=True)
        child.close()
        for handle, name in ((parent, 'rpc'), (process.stdout, 'stdout'), (process.stderr, 'stderr')):
            os.set_blocking(handle.fileno(), False)
            selector.register(handle, selectors.EVENT_READ, name)
        send({'options': options, 'call': call})
        while selector.get_map():
            for key, _ in selector.select(min(remaining(), .1)):
                data = os.read(key.fd, 65536)
                if not data:
                    selector.unregister(key.fileobj)
                    continue
                if key.data != 'rpc':
                    channel = outputs[key.data]
                    capacity = options.max_output_bytes - sum(len(value) for value in outputs.values())
                    channel.extend(data[:max(0, capacity)])
                    if len(data) > capacity:
                        raise ProtocolFailure('candidate stdout/stderr exceeds the declared byte limit')
                    continue
                if terminal is not None:
                    raise ProtocolFailure('candidate sent data after its terminal reply')
                buffer.extend(data)
                while b'\n' in buffer:
                    line, _, rest = buffer.partition(b'\n')
                    buffer = bytearray(rest)
                    if len(line) + 1 > options.max_frame_bytes:
                        raise ProtocolFailure('incoming RPC frame exceeds the declared byte limit')
                    try:
                        message = read_json(line.decode('utf-8'))
                    except (ValueError, UnicodeError) as exc:
                        raise ProtocolFailure('candidate sent invalid RPC JSON') from exc
                    kind = message.get('kind') if isinstance(message, dict) else None
                    if kind == 'model':
                        if terminal is not None or set(message) != {'kind', 'ordinal', 'prompt'} or type(message['ordinal']) is not int:
                            raise ProtocolFailure('invalid model request')
                        ordinal += 1
                        if message['ordinal'] != ordinal or type(message['prompt']) is not str or not message['prompt'].strip():
                            raise ProtocolFailure('invalid model request identity or prompt')
                        if ordinal > options.max_model_requests:
                            raise ProtocolFailure('candidate exceeded its declared model request limit')
                        try:
                            result = actor(message['prompt'], timeout_seconds=remaining())
                        except RecoveryRequired:
                            raise
                        except Exception as exc:
                            raise GatewayFailure(f'{type(exc).__name__}: {exc}') from exc
                        # A failed gateway response never becomes candidate-authored text.
                        if result.status != OperationStatus.COMPLETED:
                            raise GatewayFailure(result.diagnostic or 'managed model call failed')
                        event = {'kind': 'model', 'ordinal': ordinal, 'prompt': message['prompt'],
                                 'text': result.text, 'runtime': result.runtime.to_dict(), 'usage': result.usage.to_dict()}
                        trace_bytes += len(canonical_json(event).encode())
                        if trace_bytes > options.max_trace_bytes:
                            raise ProtocolFailure('candidate exceeded its declared model trace byte limit')
                        events.append(event)
                        send({'kind': 'model_result', 'ordinal': ordinal, 'text': result.text})
                    elif kind in ('result', 'error'):
                        expected = {'kind', 'output', 'state'} if kind == 'result' else {'kind', 'diagnostic'}
                        if terminal is not None or set(message) != expected:
                            raise ProtocolFailure('invalid terminal RPC reply')
                        terminal = message
                        if kind == 'error':
                            if type(message['diagnostic']) is not str:
                                raise ProtocolFailure('invalid candidate diagnostic')
                            status, diagnostic = Outcome.INVALID_CANDIDATE, message['diagnostic']
                    else:
                        raise ProtocolFailure('unknown candidate RPC message')
                if len(buffer) > options.max_frame_bytes:
                    raise ProtocolFailure('incoming RPC frame exceeds the declared byte limit')
        process.wait(timeout=remaining())
        remaining()
        if buffer or terminal is None or (process.returncode != 0 and terminal['kind'] != 'error'):
            raise ProtocolFailure('candidate exited without a complete successful protocol exchange')
    except (TimeoutError, subprocess.TimeoutExpired):
        status, diagnostic = Outcome.TIMED_OUT, 'candidate code invocation timed out'
    except ProtocolFailure as exc:
        status, diagnostic = Outcome.INVALID_CANDIDATE, str(exc)
    except GatewayFailure as exc:
        status, diagnostic = Outcome.INFRA_ERROR, str(exc)
    finally:
        # Also kill descendants that outlive a successful parent or retain pipe FDs.
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            process.stdout.close()
            process.stderr.close()
        selector.close()
        parent.close()
        child.close()
    events.append({'kind': 'process', 'stdout': outputs['stdout'].decode('utf-8', errors='replace'),
                   'stderr': outputs['stderr'].decode('utf-8', errors='replace'),
                   'pid': process.pid if process else None,
                   'exit_code': process.returncode if process else None})
    success = status == Outcome.SUCCEEDED
    return Reply(status=status, output=terminal['output'] if success else None,
                 state=terminal['state'] if success else call.state.value, trace=Trace(events=tuple(events)), diagnostic=diagnostic)
