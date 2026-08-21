"""The self-evolution loop: run a harness for N context-fresh episodes and record its
trajectory.

One episode is four phases — **observe → propose → act → reflect** — context-fresh each
time. Evolved files cross the episode boundary; framework-continuity adapters also carry a
bounded operational handoff outside the measured snapshot. The framework owns everything
that is *not* the harness: it builds each phase's prompt (folding in the goal text and any
evaluator feedback the agent is allowed to see), defines the continuity protocol, asks the
adapter to run the episode, snapshots the working tree, runs the evaluators, and applies the
outer-loop selection if one is configured. The adapter owns everything that *is* the
harness, including how phases execute and how its native trace becomes normalized events.

This separation is what makes Proteus harness-agnostic and condition-complete at once: the
same framework runs Aki or a bare ReAct loop, under no-goal or multi-goal, with evaluators
hidden or visible, and the measurement layer reads all of it with one ruler.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from proteus.core import snapshot
from proteus.core.adapter import EpisodeSpec, HarnessAdapter
from proteus.core.disposition import Disposition
from proteus.core.goal import GoalConfig, GoalContext

PHASES = ("observe", "propose", "act", "reflect")

BASE_PROMPTS: Mapping[str, str] = {
    "observe": (
        "Take stock of the harness you woke up in: what is here, what state it is in, "
        "and what evidence is relevant to the objective."
    ),
    "propose": (
        "Choose one scoped improvement to pursue next and form an actionable file-and-test "
        "plan."
    ),
    "act": (
        "Carry out the scoped plan by editing your own harness."
    ),
    "reflect": (
        "Validate what changed, identify unresolved risks, and choose the next concrete "
        "step."
    ),
}


@dataclass
class RunConfig:
    name: str
    adapter: HarnessAdapter
    disposition: Disposition
    goal: GoalConfig
    root: Path
    model: str
    episodes: int = 30
    max_turns: int = 100
    min_turns_per_phase: int = 0
    """Per-phase floor on the turn budget (see EpisodeSpec). `max_turns` must be at
    least `len(PHASES) * min_turns_per_phase`."""
    seed: int = 0
    task: object | None = None
    """A `proteus.bench.BenchTask` to seed before episode 1. Its workspace is
    `<run>/task/`, beside the measured `<run>/harness/` and outside the snapshot. An
    adapter that supports benchmark work must expose that sibling to its agent; dsh/pi
    mount it at `/workspace/task`."""
    announce_budget: bool = False
    """Tell the agent its per-episode budget (`max_turns`) in every phase prompt, so it
    can plan within it. Off by default: announcing the budget changes behaviour — that is
    the point — so it is an experimental condition, recorded in the manifest, not a
    silent default. Enforcement is separate and always on where possible: hard cap for
    in-process harnesses, between-phase budget checks and mid-phase log watching for
    containerized ones."""
    progress_path: Path | None = None
    """Where to append one JSON line per finished episode (live tracking). Must live
    OUTSIDE `root`: the subject agent can read its own run root, and a progress record
    carries the condition label and HIDDEN evaluator scores."""


@dataclass
class RunResult:
    name: str
    episodes_complete: int
    root: str
    error: str = ""
    eval_history: list[dict] = field(default_factory=list)
    counters: dict = field(default_factory=dict)
    """Numeric adapter counters summed across episodes (tokens_in/tokens_out where the
    adapter reports them) — what a cost estimate is built from."""


def _phase_prompts(cfg: RunConfig, prior_feedback: str) -> dict[str, str]:
    """Assemble the four phase texts for one episode from the base prompts + disposition +
    goal + (visible) evaluator feedback. The agent never sees anything about why."""
    prompts = dict(BASE_PROMPTS)
    from proteus.core.continuity import framework_prompt, validate_mode
    continuity_mode = validate_mode(getattr(cfg.adapter, "continuity_mode", "native"))
    if continuity_mode == "framework":
        for ph in PHASES:
            prompts[ph] = f"{prompts[ph]}\n\n{framework_prompt(ph)}"
    # Phases are context-fresh.  Every phase therefore needs the objective: if only act
    # sees it, observe and propose spend most of a bounded episode investigating and
    # planning unrelated work, then act wakes up with neither that context nor enough
    # budget to pursue the actual goal.  Empty text preserves the no-goal condition.
    gt = cfg.goal.goal_text()
    if gt:
        objective = f"Evolution objective for this run:\n{gt}"
        for ph in PHASES:
            prompts[ph] = f"{objective}\n\n{prompts[ph]}"
    # evaluator feedback the agent is allowed to see enters the observe phase
    if prior_feedback:
        prompts["observe"] = f"{prior_feedback}\n\n{prompts['observe']}"
    # the budget announcement comes first: it frames how the agent plans the episode
    if cfg.announce_budget and cfg.max_turns:
        note = (f"Budget: you have at most {cfg.max_turns} tool calls in this episode, "
                "across all phases. Plan within it; the episode ends when it is spent.")
        if cfg.min_turns_per_phase:
            note += (f" Each later phase reserves at least {cfg.min_turns_per_phase} "
                     "of them; a phase may be ended early to protect that reserve.")
        for ph in PHASES:
            prompts[ph] = f"{note}\n\n{prompts[ph]}"
    # the disposition contributes its (per-phase) text — unless the adapter already carries
    # it in a file the harness loads itself, in which case adding it here would deliver the
    # same perturbation twice per phase (see HarnessAdapter.disposition_in_files)
    if not getattr(cfg.adapter, "disposition_in_files", False):
        for ph in PHASES:
            suffix = cfg.disposition.phase_text(ph)
            if suffix:
                prompts[ph] = f"{prompts[ph]}\n\n{suffix}"
    return prompts


def _append_progress(cfg: RunConfig, ep: int, res, trace, accepted: bool, results) -> None:
    """One JSON line per finished episode, for the live report. Never inside cfg.root."""
    import time

    from proteus.measure import distance
    units = distance.units(cfg.root / "harness", cfg.adapter.surfaces())
    rec = {
        "ts": time.time(), "name": cfg.name, "seed": cfg.seed, "episode": ep,
        "episodes_target": cfg.episodes, "ok": res.ok, "turns": res.turns,
        "tool_calls": sum(1 for e in trace if e.tool),
        "units": {k: len(v) for k, v in units.items()},
        "accepted": accepted,
        "scores": {r.name: r.score for r in results},
        "counters": dict(res.counters or {}),
    }
    cfg.progress_path.parent.mkdir(parents=True, exist_ok=True)
    with cfg.progress_path.open("a", encoding="utf-8") as sink:
        sink.write(json.dumps(rec) + "\n")


def completed_episodes(cfg: RunConfig) -> int:
    """Contiguous episodes already snapshotted under `cfg.root`, for mid-seed resume.

    Counts commits, not trace files: a provider outage writes a trace per failed attempt,
    and counting those reports a seed that never finished an episode as complete. Counting
    is contiguous from 1 because a gap means the chain the measurement walks is broken.
    """
    harness = cfg.root / "harness"
    if not (harness.parent / ".snapshot.git").exists():
        return 0
    ep = 0
    while snapshot.commit_for_episode(harness, ep + 1) is not None:
        ep += 1
    return ep


def run(cfg: RunConfig, start: int = 0) -> RunResult:
    """Run one seed's full trajectory, harness retained under `cfg.root`.

    `start` resumes an interrupted seed: episodes up to and including `start` are taken as
    done and the harness on disk is used as-is. Re-seeding a resumed root would overwrite
    the evolved harness with fresh templates, so provisioning is skipped entirely — at
    tens of minutes per episode, restarting a seed that died at episode 26 throws away
    real trajectory, which is why this exists.
    """
    harness = cfg.root / "harness"
    if cfg.max_turns and cfg.min_turns_per_phase * len(PHASES) > cfg.max_turns:
        raise ValueError(
            f"max_turns={cfg.max_turns} cannot honour min_turns_per_phase="
            f"{cfg.min_turns_per_phase}: {len(PHASES)} phases need at least "
            f"{cfg.min_turns_per_phase * len(PHASES)} turns")
    if start:
        if completed_episodes(cfg) < start:
            raise ValueError(
                f"cannot resume {cfg.root} at episode {start}: only "
                f"{completed_episodes(cfg)} episodes are snapshotted there")
    else:
        cfg.adapter.seed(harness, cfg.seed)
        cfg.adapter.install_disposition(harness, cfg.disposition)
        if cfg.task is not None:
            from proteus.bench.task import seed_task
            seed_task(harness, cfg.task)
        snapshot.init(harness)

    # Resume must restore the experiment's state, not just its files: the selection
    # baseline, the visible feedback, and the cumulative counters all live in
    # eval_history, and a resume that reset them would let accept_reject approve a
    # post-resume episode worse than everything before the interruption.
    eval_history: list[dict] = []
    prior_feedback = ""
    totals: dict = {}
    best_score: float | None = None
    history_path = cfg.root / "eval_history.json"
    if start and history_path.exists():
        try:
            eval_history = json.loads(history_path.read_text())[:start]
        except (json.JSONDecodeError, OSError):
            eval_history = []
        for row in eval_history:
            results = row.get("results") or []
            if results:
                score = sum(r.get("score", 0.0) for r in results) / len(results)
                if row.get("accepted") and (best_score is None or score >= best_score):
                    best_score = score
            for key, value in (row.get("counters") or {}).items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    totals[key] = totals.get(key, 0) + value
        if eval_history:
            last = eval_history[-1]
            from proteus.core.goal import EvalResult
            by_name = {r["name"]: EvalResult(**r) for r in (last.get("results") or [])}
            prior_feedback = cfg.goal.observe_feedback(by_name)
            if prior_feedback and not last.get("accepted", True):
                prior_feedback += "\n(Your last episode's changes were not kept.)"
    error = ""
    done = start
    last_accepted = snapshot.head(harness)   # episode-0 state, or the resume point
    for ep in range(start + 1, cfg.episodes + 1):
        spec = EpisodeSpec(
            root=cfg.root, episode=ep, model=cfg.model,
            phase_prompts=_phase_prompts(cfg, prior_feedback),
            max_turns=cfg.max_turns, seed=cfg.seed,
            min_turns_per_phase=cfg.min_turns_per_phase,
            continuity_mode=getattr(cfg.adapter, "continuity_mode", "native"),
        )
        try:
            res = cfg.adapter.run_episode(spec)
        except Exception as exc:  # noqa: BLE001 - a failed episode is a record, not a crash
            error = f"{type(exc).__name__}: {exc}"
            break
        if not res.ok:
            error = res.error
            break

        # evaluate the episode BEFORE snapshotting, so selection can still reject it.
        # an evaluator is user (or benchmark) code — a crash in it must not take the whole
        # trajectory down; a failed evaluator records a zero and the run continues.
        trace = cfg.adapter.read_trace(cfg.root, ep)
        try:
            results = cfg.goal.evaluate(trace, GoalContext(str(harness), ep))
        except Exception as exc:  # noqa: BLE001
            from proteus.core.goal import EvalResult
            results = [EvalResult(name="evaluator-error", score=0.0,
                                  detail=f"{type(exc).__name__}: {exc}"[:200])]
        by_name = {r.name: r for r in results}

        # outer-loop selection on the scores (visibility-independent: an outer loop may
        # act on scores the agent itself never sees)
        accepted = True
        if cfg.goal.selection == "accept_reject" and results:
            score = sum(r.score for r in results) / len(results)
            if best_score is not None and score < best_score:
                accepted = False
            else:
                best_score = score

        if accepted:
            last_accepted = snapshot.commit(harness, f"episode {ep}: {cfg.name}")
        else:
            # non-destructive rejection: the rejected candidate tree goes into history
            # first (as "candidate N:", outside the episode->commit mapping), then the
            # restore is committed as episode N so the mapping stays gapless
            snapshot.commit(harness, f"candidate {ep}: {cfg.name} [rejected]")
            snapshot.restore(harness, last_accepted)
            snapshot.commit(harness, f"episode {ep}: {cfg.name} [rejected]")
        done = ep
        for key, value in (res.counters or {}).items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                totals[key] = totals.get(key, 0) + value

        eval_history.append({"episode": ep, "accepted": accepted,
                             "results": [r.__dict__ for r in results],
                             "counters": dict(res.counters or {})})
        prior_feedback = cfg.goal.observe_feedback(by_name)  # OBSERVE-visible only
        if prior_feedback and not accepted:
            prior_feedback += "\n(Your last episode's changes were not kept.)"

        if cfg.progress_path is not None:
            _append_progress(cfg, ep, res, trace, accepted, results)

    history_path.write_text(json.dumps(eval_history, indent=1))
    return RunResult(name=cfg.name, episodes_complete=done, root=str(cfg.root),
                     error=error, eval_history=eval_history, counters=totals)
