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
from pathlib import Path

from proteus.bench._datasets import download_verified
from proteus.bench._isolation import cleanup_driver, build_driver_source, install_driver
from proteus.bench.task import BenchTask
from proteus.core.goal import EvalResult

DATA_URL = (
    "https://raw.githubusercontent.com/openai/human-eval/"
    "6d43fb980f9fee3c892a914eda09951f772ad10d/data/HumanEval.jsonl.gz"
)
DATA_SHA256 = "b796127e635a67f93fb35c04f4cb03cf06f38c8072ee7cee8833d7bee06979ef"
GRADE_TIMEOUT_S = 60
CALL_TIMEOUT_S = 15

_DRIVER_BODY = '''\
try:
    Path(__file__).unlink()
    namespace = {"__name__": "__main__"}
    exec(PROMPT_SOURCE, namespace)
    candidate = _RemoteFunction(ENTRY_POINT)
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
    return download_verified(
        name="HumanEval",
        url=DATA_URL,
        expected_sha256=DATA_SHA256,
        cache=cache,
        validate=_records,
    )


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
    driver = ws / "_grade.py"
    source = build_driver_source(
        report_prefix=report_prefix,
        worker_prefix=worker_prefix,
        call_timeout_s=CALL_TIMEOUT_S,
        bindings={
            "PROMPT_SOURCE": spec["prompt"],
            "TEST_SOURCE": spec["test"],
            "ENTRY_POINT": spec["entry_point"],
        },
        body=_DRIVER_BODY,
    )
    try:
        install_driver(driver, source)
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
