"""Built-in evaluators — small, offline, and composable.

An `Evaluator` is any callable `(trace, ctx) -> EvalResult`; these cover the common cases
so tests, examples, and quick experiments need no custom code. Benchmark verifiers and LLM
judges plug in the same way — write a callable and wrap it in an `EvaluatorSpec` (or use
the compatibility `Goal` API).
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from proteus.core.adapter import ActionEvent, Surface
from proteus.core.goal import EvalResult, GoalContext


def surface_units(surface: Surface | str, name: str = "", target: int = 0):
    """Score = number of units on a declared surface or harness-relative path.

    An intrinsic, harness-readable measure: no model, no network. With `target`, score is
    capped there and `passed` flips when reached. A declared `Surface` uses its `unit`
    contract (file, directory, or top-level definition); a string retains the original
    compatibility behavior of counting a file as one or recursively counting files.
    """
    surface_name = surface.name if isinstance(surface, Surface) else str(surface)
    surface_subdir = surface.subdir if isinstance(surface, Surface) else str(surface)
    label = name or f"units:{surface_name}"

    def evaluate(trace: Sequence[ActionEvent], ctx: GoalContext) -> EvalResult:
        harness_root = Path(ctx.harness_root)
        if isinstance(surface, Surface):
            from proteus.measure import distance
            n = len(distance.units(harness_root, (surface,))[surface.name])
        else:
            root = harness_root / surface_subdir
            if root.is_file():
                n = 1
            elif root.is_dir():
                n = sum(1 for p in root.rglob("*") if p.is_file())
            else:
                n = 0
        score = float(min(n, target) if target else n)
        return EvalResult(name=label, score=score,
                          passed=bool(target and n >= target),
                          detail=f"{n} unit{'s' if n != 1 else ''} on {surface_subdir}")
    return evaluate


def tool_calls(name: str = "tool-calls", ceiling: int = 0):
    """Score = number of tool calls in the episode's trace (activity, not quality).

    With `ceiling`, score is `ceiling - calls` — an economy objective: fewer calls score
    higher, which is the shape reward-hacking studies need.
    """

    def evaluate(trace: Sequence[ActionEvent], ctx: GoalContext) -> EvalResult:
        calls = sum(1 for e in trace if e.tool)
        score = float(ceiling - calls) if ceiling else float(calls)
        return EvalResult(name=name, score=score, detail=f"{calls} tool calls")
    return evaluate


def file_contains(relpath: str, needle: str, name: str = ""):
    """Pass/fail: does `harness/<relpath>` exist and contain `needle`? A minimal task
    verifier — the shape a benchmark check takes."""
    label = name or f"contains:{relpath}"

    def evaluate(trace: Sequence[ActionEvent], ctx: GoalContext) -> EvalResult:
        root = Path(ctx.harness_root).resolve()
        p = (root / relpath).resolve()
        # confine to the harness root: relpath is caller-supplied and, on the hosted lab,
        # attacker-supplied — an absolute or ../ path would turn this into a file-read
        # oracle over the host (existence + substring membership via the returned score).
        if not (p == root or root in p.parents):
            return EvalResult(name=label, score=0.0, passed=False,
                              detail=f"{relpath} escapes the harness root")
        ok = p.is_file() and needle in p.read_text(encoding="utf-8", errors="replace")
        return EvalResult(name=label, score=1.0 if ok else 0.0, passed=ok,
                          detail=f"{relpath} {'contains' if ok else 'missing'} target")
    return evaluate


def structural_step(surfaces, name: str = "structural-step"):
    """Measurement evaluator: units added/dropped/revised since the previous episode.

    This is the study's own ruler run between episodes instead of after the sweep — the
    same per-surface delta `proteus measure --travel` sums, surfaced while the run is
    live so the tracking page shows movement, and available to the agent under OBSERVE
    (which is itself an experimental condition: an agent that can see its own structural
    churn). Reads the run's snapshot chain; episode 1 compares the current candidate with
    the episode-0 seed, and later episodes compare with the preceding episode snapshot.
    """

    def evaluate(trace: Sequence[ActionEvent], ctx: GoalContext) -> EvalResult:
        from proteus.measure import distance
        root = Path(ctx.harness_root)
        if ctx.active_harness_root:
            prev = Path(ctx.active_harness_root)
            deltas = distance.compare(prev, root, surfaces)
            moved = sum(d.added + d.dropped + d.revised for d in deltas.values())
            parts = [f"{s}: +{d.added}/-{d.dropped}/~{d.revised}"
                     for s, d in deltas.items() if d.added or d.dropped or d.revised]
            return EvalResult(name=name, score=float(moved),
                              detail="; ".join(parts) or "no structural change")
        import tempfile

        from proteus.core import snapshot
        prev_sha = snapshot.commit_for_episode(root, ctx.episode - 1)
        if prev_sha is None:
            return EvalResult(name=name, score=0.0, detail="no previous snapshot")
        with tempfile.TemporaryDirectory() as tmp:
            prev = Path(tmp) / "prev"
            snapshot.materialize(root, prev_sha, prev)
            deltas = distance.compare(prev, root, surfaces)
        moved = sum(d.added + d.dropped + d.revised for d in deltas.values())
        parts = [f"{s}: +{d.added}/-{d.dropped}/~{d.revised}"
                 for s, d in deltas.items() if d.added or d.dropped or d.revised]
        return EvalResult(name=name, score=float(moved),
                          detail="; ".join(parts) or "no structural change")
    return evaluate
