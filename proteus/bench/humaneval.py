"""OpenAI HumanEval as lightweight external benchmark tasks.

The MIT-licensed dataset from ``openai/human-eval`` is not vendored. Public prompts are
seeded into the task workspace; canonical solutions and tests remain held out.
"""

from __future__ import annotations

import gzip
import json
import os
import secrets
import subprocess
import tempfile
from pathlib import Path
from urllib import request

from proteus.bench.task import BenchTask
from proteus.core.goal import EvalResult

DATA_URL = (
    "https://raw.githubusercontent.com/openai/human-eval/master/data/HumanEval.jsonl.gz"
)
GRADE_TIMEOUT_S = 60
CALL_TIMEOUT_S = 15

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
    raise ValueError(f"unknown HumanEval value type: {kind!r}")
'''

_WORKER = _CODEC + '''\
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
emit = sys.stdout.write
flush = sys.stdout.flush
exit_now = os._exit
here = Path.cwd()
solution = here / "solution.py"
namespace = {"__name__": "__main__", "__file__": str(solution),
             "__builtins__": dict(vars(builtins))}
try:
    request = json_loads(sys.argv[1])
    source = solution.read_text(encoding="utf-8")
    trusted_exec(trusted_compile(source, str(solution), "exec"), namespace)
    function = namespace[ENTRY_POINT]
    value = function(*_decode(request["args"]), **_decode(request["kwargs"]))
    response = {"ok": True, "value": _encode(value)}
except caught as exc:
    response = {"ok": False, "error": f"{_type(exc).__name__}: {exc}"}
emit(WORKER_PREFIX + json_dumps(response, separators=(",", ":")) + "\\n")
flush()
exit_now(0)
'''

_DRIVER = _CODEC + '''\
import json
import os
import subprocess
import sys
from pathlib import Path

caught = BaseException
emit = sys.stdout.write
flush = sys.stdout.flush
exit_now = os._exit
here = Path.cwd()

def _remote_call(args, kwargs):
    request = json.dumps(
        {"args": _encode(args), "kwargs": _encode(kwargs)},
        separators=(",", ":"),
    )
    proc = subprocess.run(
        [sys.executable, "-c", WORKER_SOURCE, request],
        cwd=here,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=CALL_TIMEOUT_S,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError("candidate worker failed")
    reports = [line for line in proc.stdout.splitlines() if line.startswith(WORKER_PREFIX)]
    if not reports:
        raise RuntimeError("candidate worker produced no report")
    response = json.loads(reports[-1][len(WORKER_PREFIX):])
    if not response.get("ok"):
        raise RuntimeError(response.get("error", "candidate call failed"))
    return _decode(response["value"])

class _RemoteFunction:
    def __call__(self, *args, **kwargs):
        return _remote_call(args, kwargs)

try:
    Path(__file__).resolve().unlink()
    namespace = {"__name__": "__main__"}
    exec(PROMPT_SOURCE, namespace)
    candidate = _RemoteFunction()
    namespace[ENTRY_POINT] = candidate
    exec(TEST_SOURCE, namespace)
    namespace["check"](candidate)
    passed = True
except caught:
    passed = False
emit(REPORT_PREFIX + ("pass" if passed else "fail") + "\\n")
flush()
exit_now(0)
'''


def dataset_path(dataset_file: str | os.PathLike | None = None) -> Path:
    """Resolve an explicit, environment-provided, or cached HumanEval dataset."""
    if dataset_file:
        return Path(dataset_file).expanduser()
    env = os.environ.get("PROTEUS_HUMANEVAL_PATH")
    if env:
        return Path(env).expanduser()
    cache = Path.home() / ".cache" / "proteus" / "humaneval" / "HumanEval.jsonl.gz"
    if not cache.exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        with request.urlopen(DATA_URL, timeout=30) as response:
            payload = response.read()
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=cache.parent, suffix=".jsonl.gz", delete=False
            ) as tmp:
                tmp.write(payload)
                temp_path = Path(tmp.name)
            _records(temp_path)
            temp_path.replace(cache)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
    return cache


def _records(path: Path) -> dict[str, dict]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as stream:
        return {
            row["task_id"]: row
            for line in stream
            if line.strip()
            for row in [json.loads(line)]
        }


def list_tasks(dataset_file: str | os.PathLike | None = None) -> list[str]:
    """Task IDs available in the HumanEval dataset."""
    return list(_records(dataset_path(dataset_file)))


def _load(path: Path, task_id: str) -> dict:
    try:
        return _records(path)[task_id]
    except KeyError:
        raise KeyError(
            f"unknown HumanEval task {task_id!r}; see proteus.bench.humaneval.list_tasks()"
        ) from None


def _setup(ws: Path, spec: dict) -> None:
    (ws / "README.md").write_text(
        f"# {spec['task_id']}\n\nImplement the function in `solution.py`.\n",
        encoding="utf-8",
    )
    (ws / "solution.py").write_text(spec["prompt"], encoding="utf-8")


def _grade(ws: Path, spec: dict, name: str, *, sandbox=None) -> EvalResult:
    report_prefix = f"PROTEUS_HUMANEVAL_RESULT:{secrets.token_hex(16)}:"
    worker_prefix = f"PROTEUS_HUMANEVAL_VALUE:{secrets.token_hex(16)}:"
    worker_source = (
        f"ENTRY_POINT = {spec['entry_point']!r}\nWORKER_PREFIX = {worker_prefix!r}\n"
        + _WORKER
    )
    driver = ws / "_grade.py"
    driver.write_text(
        f"PROMPT_SOURCE = {spec['prompt']!r}\n"
        f"TEST_SOURCE = {spec['test']!r}\n"
        f"ENTRY_POINT = {spec['entry_point']!r}\n"
        f"REPORT_PREFIX = {report_prefix!r}\n"
        f"WORKER_PREFIX = {worker_prefix!r}\n"
        f"WORKER_SOURCE = {worker_source!r}\n"
        f"CALL_TIMEOUT_S = {CALL_TIMEOUT_S}\n"
        + _DRIVER,
        encoding="utf-8",
    )
    try:
        from proteus.bench.sandbox import run_python

        proc = run_python(
            ws, "_grade.py", timeout_s=GRADE_TIMEOUT_S, sandbox=sandbox, isolated=True
        )
    except subprocess.TimeoutExpired:
        return EvalResult(
            name=name,
            score=0.0,
            passed=False,
            detail=f"grading timed out after {GRADE_TIMEOUT_S}s",
        )
    finally:
        driver.unlink(missing_ok=True)

    stdout = getattr(proc, "stdout", "")
    stderr = getattr(proc, "stderr", "")
    stdout = stdout if isinstance(stdout, str) else ""
    stderr = stderr if isinstance(stderr, str) else ""
    reports = [line for line in stdout.splitlines() if line.startswith(report_prefix)]
    if getattr(proc, "returncode", 1) != 0 or not reports:
        diagnostic = (stderr or stdout)[-200:]
        return EvalResult(
            name=name,
            score=0.0,
            passed=False,
            detail=f"grader produced no report: {diagnostic}",
        )

    passed = reports[-1][len(report_prefix):] == "pass"
    return EvalResult(
        name=name,
        score=1.0 if passed else 0.0,
        passed=passed,
        detail=f"official check {'passed' if passed else 'failed'}",
    )


def humaneval_task(
    task_id: str, dataset_file: str | os.PathLike | None = None
) -> BenchTask:
    """Create one HumanEval problem as a ``BenchTask``."""
    spec = _load(dataset_path(dataset_file), task_id)
    name = f"humaneval:{spec['task_id']}"
    return BenchTask(
        id=name,
        goal_text=(
            f"Solve {spec['task_id']} in `task/solution.py`; official tests are held out."
        ),
        setup=lambda ws, s=spec: _setup(ws, s),
        grade=lambda ws, sandbox=None, s=spec, n=name: _grade(
            ws, s, n, sandbox=sandbox
        ),
    )
