"""Aider Polyglot exercises as goals — the lightest *external* benchmark, and the template.

Three tiers of benchmark evaluator, by weight:

    local.py     built-in seeded bugs   offline, zero setup       smoke tests, plumbing
    polyglot.py  Exercism exercises     one git clone, no Docker  THE tier to start from
    swe.py       SWE-bench              ~120 GB of images, x86_64 headline numbers

This module is deliberately the worked example for contributing a benchmark. The whole
contract is one `BenchTask`: `setup(ws)` writes the task into the agent's workspace,
`grade(ws)` scores whatever is there, `goal_text` says what to pursue. Everything else
(seeding into the harness, visibility, selection, records) is the framework's job. If
your benchmark can be expressed as those three things, it plugs in — see `docs/BENCHMARKS.md`
for the checklist.

Dataset: github.com/Aider-AI/polyglot-benchmark — Exercism exercises (attribution:
© Exercism, per that repo's README). The Python track needs nothing beyond a Python
interpreter: grading runs the exercise's own unittest suite through a stdlib driver, so
there is no pytest dependency, no container, and no dataset download beyond one shallow
clone on first use (set `PROTEUS_POLYGLOT_DIR` to use an existing checkout instead).

Two properties every graded task here preserves, copied from `local.py` because agents
rediscover the same exploits:

  * **Held-out tests.** `grade` rewrites the test files from the dataset before running
    them, so editing the tests in the workspace cannot raise the score.
  * **cwd-independent grading.** The driver resolves files beside itself, never against
    the working directory, so the same driver runs on the host and inside a container
    whose cwd is `/`.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from proteus.bench.task import BenchTask
from proteus.core.goal import EvalResult

REPO_URL = "https://github.com/Aider-AI/polyglot-benchmark"
GRADE_TIMEOUT_S = 120

#: The stdlib test driver, written into the workspace at grade time. unittest, not
#: pytest: Exercism's Python tests are unittest.TestCase classes, and a benchmark tier
#: whose point is "no dependencies" should not acquire one to count its own results.
_DRIVER = '''\
import importlib.util, json, os, sys, unittest
here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, here)
suite = unittest.TestSuite()
loader = unittest.defaultTestLoader
for fname in json.loads(os.environ.get("PROTEUS_TEST_FILES", "[]")):
    spec = importlib.util.spec_from_file_location(fname[:-3], os.path.join(here, fname))
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception:
        print(json.dumps({"total": 1, "failed": 1, "error": "<import>"}))
        sys.exit(0)
    suite.addTests(loader.loadTestsFromModule(mod))
res = unittest.TextTestRunner(stream=open(os.devnull, "w"), verbosity=0).run(suite)
print(json.dumps({"total": res.testsRun,
                  "failed": len(res.failures) + len(res.errors)}))
'''


def dataset_dir(repo_dir: str | os.PathLike | None = None) -> Path:
    """Resolve the benchmark checkout: explicit arg > $PROTEUS_POLYGLOT_DIR > a cached
    shallow clone under ~/.cache/proteus (cloned on first use — network, once)."""
    if repo_dir:
        return Path(repo_dir).expanduser()
    env = os.environ.get("PROTEUS_POLYGLOT_DIR")
    if env:
        return Path(env).expanduser()
    cache = Path.home() / ".cache" / "proteus" / "polyglot-benchmark"
    if not cache.exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--depth", "1", "--quiet", REPO_URL, str(cache)],
                       check=True)
    return cache


def _exercises(repo: Path) -> Path:
    return repo / "python" / "exercises" / "practice"


def list_tasks(repo_dir: str | os.PathLike | None = None) -> list[str]:
    """Exercise names available in the Python track."""
    base = _exercises(dataset_dir(repo_dir))
    if not base.is_dir():
        return []
    return sorted(p.name for p in base.iterdir() if (p / ".meta" / "config.json").is_file())


def _load(repo: Path, name: str) -> dict:
    root = _exercises(repo) / name
    cfg_path = root / ".meta" / "config.json"
    if not cfg_path.is_file():
        raise KeyError(f"unknown polyglot exercise {name!r}; "
                       f"see proteus.bench.polyglot.list_tasks()")
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    docs = []
    for doc in ("introduction.md", "instructions.md", "instructions.append.md"):
        f = root / ".docs" / doc
        if f.exists():
            docs.append(f.read_text(encoding="utf-8"))
    return {
        "root": root,
        "solution": list(cfg["files"]["solution"]),
        "test": list(cfg["files"]["test"]),
        "instructions": "\n\n".join(docs),
    }


def _setup(ws: Path, spec: dict) -> None:
    (ws / "instructions.md").write_text(spec["instructions"], encoding="utf-8")
    for rel in spec["solution"] + spec["test"]:
        dest = ws / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text((spec["root"] / rel).read_text(encoding="utf-8"), encoding="utf-8")


def _grade(ws: Path, spec: dict, name: str, *, sandbox=None) -> EvalResult:
    # held-out tests: restore from the dataset before running, so edits to the test
    # files in the workspace cannot raise the score
    for rel in spec["test"]:
        (ws / rel).write_text((spec["root"] / rel).read_text(encoding="utf-8"),
                              encoding="utf-8")
    driver = ws / "_grade.py"
    driver.write_text(_DRIVER, encoding="utf-8")
    env = {"PROTEUS_TEST_FILES": json.dumps(spec["test"])}
    try:
        from proteus.bench.sandbox import run_python
        proc = run_python(ws, "_grade.py", timeout_s=GRADE_TIMEOUT_S,
                          env=env, sandbox=sandbox)
    except subprocess.TimeoutExpired:
        return EvalResult(name=name, score=0.0, passed=False,
                          detail=f"grading timed out after {GRADE_TIMEOUT_S}s")
    finally:
        driver.unlink(missing_ok=True)
    try:
        rep = json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return EvalResult(name=name, score=0.0, passed=False,
                          detail=f"grader produced no report: {proc.stderr[-200:]}")
    total, failed = int(rep.get("total", 0)), int(rep.get("failed", 0))
    score = (total - failed) / total if total else 0.0
    return EvalResult(name=name, score=score, passed=(total > 0 and failed == 0),
                      detail=f"{total - failed}/{total} tests pass")


def polyglot_task(name: str, repo_dir: str | os.PathLike | None = None) -> BenchTask:
    """One Exercism exercise as a `BenchTask` (Python track).

    The agent gets the instructions, the stub, and the tests in its `task/` workspace;
    grading runs the exercise's own unittest suite with the tests restored from the
    dataset. Wire it like any benchmark:

        task = polyglot_task("bowling")
        cfg = RunConfig(..., goal=as_goal(task, visibility=Visibility.OBSERVE), task=task)
    """
    spec = _load(dataset_dir(repo_dir), name)
    return BenchTask(
        id=f"polyglot:{name}",
        goal_text=(spec["instructions"].strip()
                   + "\n\nThe stub and its tests are in your `task/` directory. Implement "
                     "the solution so every test passes. Do not edit the test files."),
        setup=lambda ws, s=spec: _setup(ws, s),
        grade=lambda ws, sandbox=None, s=spec, n=f"polyglot:{name}": _grade(
            ws, s, n, sandbox=sandbox),
    )
