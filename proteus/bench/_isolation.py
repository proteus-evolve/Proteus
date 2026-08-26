"""Shared source builders for benchmark parent/worker process isolation."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping


_CODEC = '''\
import base64

_type = type
_isinstance = isinstance
_bool = bool
_int = int
_float = float
_complex = complex
_str = str
_bytes = bytes
_list = list
_tuple = tuple
_set = set
_frozenset = frozenset
_dict = dict

class _OpaqueValue:
    def __init__(self, truthy):
        self.truthy = truthy

    def __bool__(self):
        return self.truthy

def _encode(value):
    kind = _type(value)
    if value is None:
        return {"type": "none"}
    if kind is _bool:
        return {"type": "bool", "value": value}
    if kind is _int:
        return {"type": "int", "value": _str(value)}
    if kind is _float:
        return {"type": "float", "value": _str(value)}
    if kind is _complex:
        return {"type": "complex", "real": _str(value.real), "imag": _str(value.imag)}
    if kind is _str:
        return {"type": "str", "value": value}
    if kind is _bytes:
        return {"type": "bytes", "value": base64.b64encode(value).decode("ascii")}
    if _isinstance(value, _list):
        return {"type": "list", "items": [_encode(item) for item in value]}
    if _isinstance(value, _tuple):
        return {"type": "tuple", "items": [_encode(item) for item in value]}
    if _isinstance(value, (_set, _frozenset)):
        return {"type": "set", "items": [_encode(item) for item in value]}
    if _isinstance(value, _dict):
        return {"type": "dict", "items": [[_encode(k), _encode(v)] for k, v in value.items()]}
    return {"type": "opaque", "truthy": _bool(value)}

def _decode(node):
    kind = node["type"]
    if kind == "none":
        return None
    if kind == "bool":
        return _bool(node["value"])
    if kind == "int":
        return _int(node["value"])
    if kind == "float":
        return _float(node["value"])
    if kind == "complex":
        return _complex(_float(node["real"]), _float(node["imag"]))
    if kind == "str":
        return _str(node["value"])
    if kind == "bytes":
        return base64.b64decode(node["value"], validate=True)
    if kind == "list":
        return [_decode(item) for item in node["items"]]
    if kind == "tuple":
        return _tuple(_decode(item) for item in node["items"])
    if kind == "set":
        return {_decode(item) for item in node["items"]}
    if kind == "dict":
        return {_decode(k): _decode(v) for k, v in node["items"]}
    if kind == "opaque":
        return _OpaqueValue(_bool(node["truthy"]))
    raise ValueError(f"unknown isolated value type: {kind!r}")
'''

EXECUTOR_SOURCE = _CODEC + '''\
import builtins
import json
import os
import sys
from pathlib import Path

trusted_exec = exec
trusted_compile = compile
caught = BaseException
json_loads = json.loads
json_dumps = json.dumps
write_result = os.write
close_result = os.close
exit_now = os._exit
here = Path.cwd()
solution = here / "solution.py"
result_fd = int(sys.argv[2])
namespace = {"__name__": "__main__", "__file__": str(solution),
             "__builtins__": dict(vars(builtins))}
try:
    request = json_loads(sys.argv[1])
    source = solution.read_text(encoding="utf-8")
    trusted_exec(trusted_compile(source, str(solution), "exec"), namespace)
    function = namespace[request["name"]]
    value = function(*_decode(request["args"]), **_decode(request["kwargs"]))
    response = {"ok": True, "value": _encode(value)}
except caught as exc:
    response = {"ok": False, "error": f"{_type(exc).__name__}: {exc}"}
payload = json_dumps(response, separators=(",", ":")).encode("utf-8")
while payload:
    payload = payload[write_result(result_fd, payload):]
close_result(result_fd)
exit_now(0)
'''

WORKER_SOURCE = _CODEC + '''\
import json
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

caught = BaseException
json_loads = json.loads
json_dumps = json.dumps
emit = sys.stdout.write
flush = sys.stdout.flush
exit_now = os._exit
here = Path.cwd()

def _terminate_process_group(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    return proc.communicate()

try:
    with tempfile.TemporaryFile() as result_stream:
        proc = subprocess.Popen(
            [sys.executable, "-c", EXECUTOR_SOURCE, sys.argv[1],
             str(result_stream.fileno())],
            cwd=here,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            pass_fds=(result_stream.fileno(),),
            start_new_session=True,
        )
        try:
            proc.communicate(timeout=float(sys.argv[2]))
        except subprocess.TimeoutExpired:
            _terminate_process_group(proc)
            raise RuntimeError("candidate executor timed out")
        result_stream.seek(0)
        payload = result_stream.read()
    if proc.returncode != 0:
        raise RuntimeError("candidate executor failed")
    if not payload:
        raise RuntimeError("candidate executor produced no result")
    candidate_response = json_loads(payload.decode("utf-8"))
    if candidate_response.get("ok"):
        response = {"ok": True, "value": _encode(_decode(candidate_response["value"]))}
    else:
        response = {"ok": False, "error": candidate_response.get(
            "error", "candidate call failed"
        )}
except caught as exc:
    response = {"ok": False, "error": f"{_type(exc).__name__}: {exc}"}
emit(WORKER_PREFIX + json_dumps(response, separators=(",", ":")) + "\\n")
flush()
exit_now(0)
'''

DRIVER_SUPPORT_SOURCE = _CODEC + '''\
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

caught = BaseException
emit = sys.stdout.write
flush = sys.stdout.flush
exit_now = os._exit
here = Path(__file__).resolve().parent

def _terminate_process_group(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    return proc.communicate()

def _remote_call(name, args, kwargs):
    request = json.dumps(
        {"name": name, "args": _encode(args), "kwargs": _encode(kwargs)},
        separators=(",", ":"),
    )
    proc = subprocess.Popen(
        [sys.executable, "-I", "-c", WORKER_SOURCE, request, str(CALL_TIMEOUT_S)],
        cwd=here,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, _ = proc.communicate(timeout=SUPERVISOR_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        _terminate_process_group(proc)
        raise RuntimeError("candidate worker timed out")
    if proc.returncode != 0:
        raise RuntimeError("candidate worker failed")
    reports = [line for line in stdout.splitlines() if line.startswith(WORKER_PREFIX)]
    if not reports:
        raise RuntimeError("candidate worker produced no report")
    response = json.loads(reports[-1][len(WORKER_PREFIX):])
    if not response.get("ok"):
        raise RuntimeError(response.get("error", "candidate call failed"))
    return _decode(response["value"])

class _RemoteFunction:
    def __init__(self, name):
        self.name = name

    def __call__(self, *args, **kwargs):
        return _remote_call(self.name, args, kwargs)

'''


def build_worker_source(worker_prefix: str) -> str:
    """Return a self-contained candidate worker with a private result prefix."""
    return (
        f"WORKER_PREFIX = {worker_prefix!r}\n"
        f"EXECUTOR_SOURCE = {EXECUTOR_SOURCE!r}\n"
        + WORKER_SOURCE
    )


def build_driver_source(
    *,
    report_prefix: str,
    worker_prefix: str,
    call_timeout_s: int,
    bindings: Mapping[str, object],
    body: str,
) -> str:
    """Bind trusted values around the common remote-call support and benchmark body."""
    worker = build_worker_source(worker_prefix)
    values = {
        **dict(bindings),
        "REPORT_PREFIX": report_prefix,
        "WORKER_PREFIX": worker_prefix,
        "WORKER_SOURCE": worker,
        "CALL_TIMEOUT_S": call_timeout_s,
        "SUPERVISOR_TIMEOUT_S": call_timeout_s + 2,
    }
    header = "".join(f"{name} = {value!r}\n" for name, value in values.items())
    return header + DRIVER_SUPPORT_SOURCE + body


def install_driver(path: Path, source: str) -> None:
    """Replace an exact file/symlink entry and create a new driver without following links."""
    path = Path(path)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(source)
    except BaseException:
        cleanup_driver(path)
        raise


def cleanup_driver(path: Path) -> OSError | None:
    """Unlink only the exact driver entry, reporting rather than raising cleanup errors."""
    try:
        Path(path).unlink()
    except FileNotFoundError:
        return None
    except OSError as exc:
        return exc
    return None
