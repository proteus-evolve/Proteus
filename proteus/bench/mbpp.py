"""Mostly Basic Python Problems (MBPP) as lightweight external benchmark tasks.

Dataset: Google Research's ``sanitized-mbpp.json``, a hand-verified subset of MBPP
released under the Apache License 2.0 in google-research/google-research. The dataset is
not vendored; it is cached on first use, or supplied with ``PROTEUS_MBPP_PATH``.

Only the prompt and an empty ``solution.py`` are seeded. Reference code and assertions
remain in the dataset for grading and are never copied into the agent's task workspace.
"""

from __future__ import annotations

import ast
import json
import os
import secrets
import subprocess
from pathlib import Path

from proteus.bench._datasets import download_verified
from proteus.bench._isolation import cleanup_driver, build_driver_source, install_driver
from proteus.bench.task import BenchTask
from proteus.core.goal import EvalResult

DATA_URL = (
    "https://raw.githubusercontent.com/google-research/google-research/"
    "e20eb00d074cdb569ee27318f112ea1e85bbb98f/mbpp/sanitized-mbpp.json"
)
DATA_SHA256 = "ca95deaa9a01ef0a6f439f88bcf0dd3db3563d22f22aad6cae04ebb9a8d8c8e9"
GRADE_TIMEOUT_S = 60
CALL_TIMEOUT_S = 15

_DRIVER_BODY = '''\
def _report(passed):
    emit(REPORT_PREFIX + str(passed) + "/" + str(len(TESTS)) + "\\n")
    flush()
    exit_now(0)

try:
    Path(__file__).unlink()
except OSError:
    _report(0)

namespace = {"__name__": "__main__"}
try:
    for statement in TEST_IMPORTS + REFERENCE_IMPORTS:
        exec(statement, namespace)
    for name in ENTRY_POINTS:
        namespace[name] = _RemoteFunction(name)
except caught:
    _report(0)

passed = 0
for assertion in TESTS:
    try:
        exec(assertion, namespace)
        passed += 1
    except caught:
        pass
_report(passed)
'''


def dataset_path(dataset_file: str | os.PathLike | None = None) -> Path:
    """Resolve an explicit, environment-provided, or cached sanitized MBPP dataset."""
    if dataset_file:
        return Path(dataset_file).expanduser()
    env = os.environ.get("PROTEUS_MBPP_PATH")
    if env:
        return Path(env).expanduser()
    cache = Path.home() / ".cache" / "proteus" / "mbpp" / "sanitized-mbpp.json"
    return download_verified(
        name="MBPP",
        url=DATA_URL,
        expected_sha256=DATA_SHA256,
        cache=cache,
        validate=_records,
    )


def _records(path: Path) -> dict[str, dict]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    return {str(row["task_id"]): row for row in rows}


def list_tasks(dataset_file: str | os.PathLike | None = None) -> list[str]:
    """Task IDs available in the sanitized dataset."""
    return sorted(_records(dataset_path(dataset_file)), key=int)


def _load(path: Path, task_id: int | str) -> dict:
    key = str(task_id)
    try:
        return _records(path)[key]
    except KeyError:
        raise KeyError(
            f"unknown MBPP task {key!r}; see proteus.bench.mbpp.list_tasks()"
        ) from None


def _setup(ws: Path, spec: dict) -> None:
    (ws / "README.md").write_text(
        f"# MBPP task {spec['task_id']}\n\n{spec['prompt'].strip()}\n\n"
        "Implement your answer in `solution.py`.\n",
        encoding="utf-8",
    )
    (ws / "solution.py").write_text(
        '"""Implement the function described in README.md."""\n', encoding="utf-8"
    )


def _entry_points(spec: dict) -> list[str]:
    tree = ast.parse(spec["code"])
    return [node.name for node in tree.body if isinstance(node, ast.FunctionDef)]


def _reference_imports(spec: dict) -> list[str]:
    source = spec["code"]
    tree = ast.parse(source)
    return [
        ast.get_source_segment(source, node) or ast.unparse(node)
        for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]


def _driver(spec: dict, report_prefix: str, worker_prefix: str) -> str:
    return build_driver_source(
        report_prefix=report_prefix,
        worker_prefix=worker_prefix,
        call_timeout_s=CALL_TIMEOUT_S,
        bindings={
            "TEST_IMPORTS": list(spec.get("test_imports", [])),
            "REFERENCE_IMPORTS": _reference_imports(spec),
            "TESTS": list(spec["test_list"]),
            "ENTRY_POINTS": _entry_points(spec),
        },
        body=_DRIVER_BODY,
    )


def _grade(ws: Path, spec: dict, name: str, *, sandbox=None) -> EvalResult:
    report_prefix = f"PROTEUS_MBPP_RESULT:{secrets.token_hex(16)}:"
    worker_prefix = f"PROTEUS_MBPP_VALUE:{secrets.token_hex(16)}:"
    driver = ws / "_grade.py"
    try:
        install_driver(driver, _driver(spec, report_prefix, worker_prefix))
    except OSError as exc:
        return EvalResult(
            name=name,
            score=0.0,
            passed=False,
            detail=f"grader setup failed ({type(exc).__name__}: {exc})",
        )

    timed_out = False
    try:
        from proteus.bench.sandbox import run_python

        proc = run_python(
            ws, "_grade.py", timeout_s=GRADE_TIMEOUT_S, sandbox=sandbox, isolated=True
        )
    except subprocess.TimeoutExpired:
        timed_out = True

    cleanup_error = cleanup_driver(driver)
    if cleanup_error is not None:
        return EvalResult(
            name=name,
            score=0.0,
            passed=False,
            detail=(
                "grader cleanup failed "
                f"({type(cleanup_error).__name__}: {cleanup_error})"
            ),
        )
    if timed_out:
        return EvalResult(
            name=name,
            score=0.0,
            passed=False,
            detail=f"grading timed out after {GRADE_TIMEOUT_S}s",
        )

    expected = len(spec["test_list"])
    stdout = getattr(proc, "stdout", "")
    stderr = getattr(proc, "stderr", "")
    stdout = stdout if isinstance(stdout, str) else ""
    stderr = stderr if isinstance(stderr, str) else ""
    try:
        reports = [line for line in stdout.splitlines() if line.startswith(report_prefix)]
        passed_raw, total_raw = reports[-1][len(report_prefix):].split("/", 1)
        passed, total = int(passed_raw), int(total_raw)
        if getattr(proc, "returncode", 1) != 0 or total != expected or not 0 <= passed <= total:
            raise ValueError("invalid MBPP grader counts")
    except (IndexError, TypeError, ValueError):
        diagnostic = (stderr or stdout)[-200:]
        return EvalResult(
            name=name,
            score=0.0,
            passed=False,
            detail=f"grader produced no report: {diagnostic}",
        )

    return EvalResult(
        name=name,
        score=passed / total if total else 0.0,
        passed=(total > 0 and passed == total),
        detail=f"{passed}/{total} tests pass",
    )


def mbpp_task(task_id: int | str, dataset_file: str | os.PathLike | None = None) -> BenchTask:
    """Create one sanitized MBPP problem as a ``BenchTask``."""
    spec = _load(dataset_path(dataset_file), task_id)
    name = f"mbpp:{spec['task_id']}"
    return BenchTask(
        id=name,
        goal_text=(
            spec["prompt"].strip()
            + "\n\nImplement the solution in `task/solution.py`; official tests are held out."
        ),
        setup=lambda ws, s=spec: _setup(ws, s),
        grade=lambda ws, sandbox=None, s=spec, n=name: _grade(ws, s, n, sandbox=sandbox),
    )
