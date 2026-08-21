"""SWE-bench as a goal: the agent fixes a real issue, the official harness grades the patch.

This is the high-fidelity end of the goal axis. One task = one SWE-bench instance; the
agent's `task/` workspace is that repository checked out at `base_commit`, its goal text is
the issue's `problem_statement`, and grading runs the instance's own container: apply the
agent's diff, apply the held-out `test_patch`, run `FAIL_TO_PASS` and `PASS_TO_PASS`.

Three facts decide whether this works, all of them load-bearing:

1. **The bridge is the diff.** The grader has its own clean checkout inside the image; the
   only thing that crosses is `git diff base_commit`. If the task workspace is not that
   repository at exactly that commit, every apply strategy fails and the score is zero.
   `setup` therefore clones and checks out; do not point this at an arbitrary directory.
2. **Cache keys ignore the patch.** The official harness caches on `(run_id, instance_id)`,
   so a stale result comes back if the run id repeats. The run id here embeds the episode.
3. **This is not a laptop workload.** Per-instance images are pulled from Docker Hub
   (hundreds of MB each over a shared base); the project asks for ~120 GB free disk, and
   arm64 coverage is partial, so grade on x86_64 Linux. Pin a *small fixed* instance set:
   every distinct instance is another image.

Scoring reports the official binary `resolved` through `passed`, and a dense
fail-to-pass fraction through `score` — a sparse 0/1 reward tells an evolution study very
little about which direction a harness moved.

Requires `pip install swebench datasets docker` (not a Proteus dependency; imported lazily
so the rest of the framework runs without them).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from proteus.bench.task import BenchTask, workspace_diff
from proteus.core.goal import EvalResult

DEFAULT_DATASET = "SWE-bench/SWE-bench_Verified"
GRADE_TIMEOUT_S = 1800


def _load_instance(instance_id: str, dataset: str, split: str) -> dict[str, Any]:
    from datasets import load_dataset
    for row in load_dataset(dataset, split=split):
        if row["instance_id"] == instance_id:
            return dict(row)
    raise KeyError(f"{instance_id} not in {dataset}:{split}")


def _setup(ws: Path, inst: dict[str, Any]) -> None:
    """Clone the instance's repository into the task workspace at `base_commit`."""
    url = f"https://github.com/{inst['repo']}.git"
    if not (ws / ".git").exists():
        subprocess.run(["git", "clone", "--quiet", url, str(ws)], check=True)
    subprocess.run(["git", "-C", str(ws), "checkout", "--quiet", inst["base_commit"]],
                   check=True)


def _grade(ws: Path, inst: dict[str, Any], episode: int = 0) -> EvalResult:
    name = f"swebench:{inst['instance_id']}"
    diff = workspace_diff(ws, inst["base_commit"])
    if not diff.strip():
        return EvalResult(name=name, score=0.0, passed=False, detail="empty patch")
    # the official harness caches on (run_id, instance_id); the run id must therefore be
    # unique per (episode, patch), or every later episode returns a stale verdict
    import hashlib
    run_id = f"proteus-ep{episode}-{hashlib.sha1(diff.encode()).hexdigest()[:10]}"

    try:
        import docker
        from swebench.harness.run_evaluation import run_instance

        # the flat `swebench.harness.test_spec` import is broken across versions
        from swebench.harness.test_spec.test_spec import make_test_spec
    except ImportError as exc:
        return EvalResult(name=name, score=0.0, passed=False,
                          detail=f"grading deps missing ({exc}); "
                                 "pip install swebench datasets docker")

    # the whole grade is guarded: run_instance's positional signature has drifted across
    # swebench majors, and this path has not been executed against an installed swebench
    # (it needs x86_64 + ~120GB). A signature mismatch or container error must degrade to
    # a scored zero with a legible message, never crash the trajectory.
    try:
        spec = make_test_spec(inst)
        pred = {"instance_id": inst["instance_id"], "model_name_or_path": "proteus",
                "model_patch": diff}
        result = run_instance(
            test_spec=spec, pred=pred, client=docker.from_env(),
            run_id=run_id,
            timeout=GRADE_TIMEOUT_S)
        if not result:
            return EvalResult(name=name, score=0.0, passed=False,
                              detail="patch did not apply, or the harness errored")
        report = result[1][inst["instance_id"]]
    except TypeError as exc:
        return EvalResult(name=name, score=0.0, passed=False,
                          detail=f"swebench run_instance signature mismatch ({exc}); "
                                 "pin a supported swebench and adjust proteus/bench/swe.py")
    except Exception as exc:  # noqa: BLE001 - container/build failure is a zero, not a crash
        return EvalResult(name=name, score=0.0, passed=False,
                          detail=f"grading error: {type(exc).__name__}: {exc}"[:200])
    resolved = bool(report.get("resolved"))
    f2p = (report.get("tests_status", {}) or {}).get(
        "FAIL_TO_PASS", {"success": [], "failure": []})
    ok, bad = len(f2p.get("success", [])), len(f2p.get("failure", []))
    dense = ok / (ok + bad) if (ok + bad) else 0.0
    return EvalResult(name=name, score=1.0 if resolved else dense, passed=resolved,
                      detail=f"resolved={resolved}; fail_to_pass {ok}/{ok + bad}")


def swe_task(instance_id: str, *, dataset: str = DEFAULT_DATASET,
             split: str = "test") -> BenchTask:
    """One SWE-bench instance as a `BenchTask`.

    The grader is episode-aware (`as_evaluator` passes the current episode) and folds a
    patch digest into the official harness's cache identity, so no episode can be served
    another episode's cached verdict.
    """
    inst = _load_instance(instance_id, dataset, split)
    return BenchTask(
        id=f"swebench:{instance_id}",
        goal_text=inst["problem_statement"],
        setup=lambda ws: _setup(ws, inst),
        grade=lambda ws, episode=0: _grade(ws, inst, episode),
        base_commit=inst["base_commit"],
    )
