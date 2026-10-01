"""Goals and evaluators: the axis Proteus spans that fixed harness-evolvers do not.

Two orthogonal axes (see the paper's no-goal argument):

  Axis A — goal presence:  none | one | many goals.
  Axis B — evaluator wiring:
      * count:       0 / 1 / N evaluators
      * visibility:  where each evaluator's score goes
          - HIDDEN:    the agent never sees it; it is used only by an outer loop
                       (accept/reject a harness version) or for offline analysis.
          - OBSERVE:   latest available evidence is shown in the next episode's fresh
                       phase contexts — the agent can react to it (feedback-seeking,
                       optimization, and the reward-hacking / Goodhart failure modes).

The `HIDDEN` mode reproduces the regime every fixed harness-evolver hard-codes
(agent-blind, offline). `OBSERVE` and the no-goal setting (no evaluator at all) are what
this framework adds. The measurement layer is identical across all settings, so a no-goal
run and a goal run are read with the same ruler.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Callable, Mapping, Sequence

from proteus.core.adapter import ActionEvent


class Visibility(str, Enum):
    HIDDEN = "hidden"        # agent never sees the score (offline / outer-loop only)
    OBSERVE = "observe"      # historical name; visible across the next episode's fresh phases


@dataclass(frozen=True)
class EvalResult:
    name: str
    score: float | None
    passed: bool = False
    detail: str = ""
    status: str = "ok"  # ok | error | missing; unavailable evidence is not a valid zero


# An evaluator scores one episode's trace (and optionally the harness state dir) into a
# result. It is a plain callable so a user can supply anything — a benchmark verifier, an
# LLM judge, a rule check, an intrinsic-novelty measure.
Evaluator = Callable[[Sequence[ActionEvent], "GoalContext"], EvalResult]


@dataclass(frozen=True)
class Goal:
    """One objective. `text` is shown to the agent in every context-fresh phase; `evaluator` scores
    progress toward it. A goal with no evaluator is a stated aim with no measured feedback;
    an evaluator with no goal text is a measured signal with no announced objective.

    Retained for compatibility; the decoupled form is `GoalConfig(text=..., evaluators=...)`
    below, where the objective is freeform text and evaluators are their own first-class
    list. Coupling them turned out to be the wrong default: what you *tell* the agent and
    what you *measure* are independent decisions, and the interesting conditions are
    exactly the ones where they differ."""

    name: str
    text: str = ""
    evaluator: Evaluator | None = None
    visibility: Visibility = Visibility.HIDDEN
    selection_eligible: bool = True

    def __post_init__(self) -> None:
        if type(self.selection_eligible) is not bool:
            raise ValueError("selection_eligible must be a boolean")


@dataclass(frozen=True)
class EvaluatorSpec:
    """One evaluator attached to a run, independent of the goal text.

    `kind` says where the signal comes from, because the two families answer different
    questions and are read differently:

      - "measurement" — the study's own instruments (structural distance, unit counts,
        behavioural statistics). Cheap, intrinsic, defined for every harness; these are
        what a no-goal run is read with.
      - "benchmark"   — an external ground truth (a local task pack, SWE-bench, a CI
        suite). Costs real compute, exists only where a task does, and is the thing a
        *specific* goal must be wired to (see `GoalConfig`).
      - "custom"      — anything else the user supplies.

    `visibility` is per evaluator: OBSERVE results are shown to the agent at the start of
    its next episode; HIDDEN results go only to the run's records (progress lines,
    eval_history, the tracking page) — the user always sees both.

    `selection_eligible` independently controls outer-loop acceptance (default True).
    Schedules default to every episode, without H0. Set `include_initial=True` for H0
    plus periodic evaluation, or `episodes=(0, 5, 10)` for an explicit schedule. Episode
    zero is evaluated on the seeded snapshot with an empty trace. `error_policy="missing"`
    stores unavailable scores as None; legacy "zero" retains 0.0 but marks status="error".
    Neither error representation is evidence for selection.
    """

    name: str
    run: Evaluator
    kind: str = "custom"                       # "measurement" | "benchmark" | "custom"
    visibility: Visibility = Visibility.HIDDEN
    selection_eligible: bool = True
    every_n_episodes: int = 1
    episodes: tuple[int, ...] | None = None
    include_initial: bool = False
    error_policy: str = "zero"

    def __post_init__(self) -> None:
        if type(self.selection_eligible) is not bool or type(self.include_initial) is not bool:
            raise ValueError("selection_eligible and include_initial must be booleans")
        if type(self.every_n_episodes) is not int or self.every_n_episodes < 1:
            raise ValueError("every_n_episodes must be a positive integer")
        if self.episodes is not None:
            if any(type(ep) is not int or ep < 0 for ep in self.episodes):
                raise ValueError("episodes must contain nonnegative integers")
            if len(set(self.episodes)) != len(self.episodes):
                raise ValueError("episodes must not contain duplicates")
            object.__setattr__(self, "episodes", tuple(sorted(self.episodes)))
            if self.every_n_episodes != 1 or self.include_initial:
                raise ValueError("episodes cannot be combined with a periodic/initial schedule")
        if self.error_policy not in ("zero", "missing"):
            raise ValueError("error_policy must be zero or missing")

    def due(self, episode: int) -> bool:
        if self.episodes is not None:
            return episode in self.episodes
        return self.include_initial if episode == 0 else episode > 0 and episode % self.every_n_episodes == 0


@dataclass(frozen=True)
class GoalContext:
    """Passed to evaluators so they can resolve the harness state if they need it."""

    harness_root: str
    episode: int
    grader_sandbox: Any = None


@dataclass(frozen=True)
class GoalConfig:
    """The full goal/evaluator condition for a run.

    The goal and the evaluators are decoupled. `text` is freeform — "make yourself more
    robust" is a legitimate objective — and `evaluators` is whatever the user wants
    measured between episodes, each with its own visibility. Due evaluators run to
    completion after episode N ends and before episode N+1 starts; OBSERVE results reach
    the agent in its next fresh phases, HIDDEN results reach only the run's records.

    Proteus does not require the user to provide a complete evaluator set: the default
    episode protocol asks the harness to judge the evidence it has and develop additional
    tests or evaluators when that would reduce uncertainty. Availability is a separate
    constraint. If a goal names an external benchmark but the harness can access neither
    its tasks nor its results, goal text alone cannot make that benchmark verifiable; give
    the harness task access or attach visible evaluator feedback.

    Presets cover the paper's conditions:
      - `GoalConfig.no_goal()`            → N0: no goal, no evaluator, no feedback.
      - `GoalConfig.single(goal, hidden)` → G-blind: one goal, offline agent-blind score.
      - `GoalConfig.single(goal, observe)`→ G-see-1: one goal, score visible in observe.
      - `GoalConfig.multi([...])`         → G-see-N / multi-goal.
    `selection` is what an OUTER loop does with hidden scores; orthogonal to visibility.
    """

    goals: tuple[Goal, ...] = ()
    selection: str = "none"            # "none" | "accept_reject" | "rank"
    text: str = ""
    """The freeform objective, decoupled from measurement. Shown in every phase."""
    evaluators: tuple[EvaluatorSpec, ...] = ()
    """Evaluators attached to this run, each with its own kind and visibility."""

    def __post_init__(self) -> None:
        names = [g.name for g in self.goals if g.evaluator is not None]
        names += [s.name for s in self.evaluators]
        if len(set(names)) != len(names):
            raise ValueError("evaluator names must be unique across goals and evaluators")
        if self.selection not in ("none", "accept_reject", "rank"):
            raise ValueError("unknown selection policy")
        # Comparing averages of changing subsets is not a meaningful selection baseline.
        if self.selection == "accept_reject":
            schedules = {(1, None, False) for g in self.goals
                         if g.evaluator is not None and g.selection_eligible}
            schedules.update((s.every_n_episodes, s.episodes, s.include_initial)
                             for s in self.evaluators if s.selection_eligible)
            if len(schedules) > 1:
                raise ValueError("selection-eligible evaluators must share one schedule")

    def selection_names(self) -> set[str]:
        return {g.name for g in self.goals if g.evaluator is not None and g.selection_eligible} | {
            s.name for s in self.evaluators if s.selection_eligible}

    def selection_score(self, results: Sequence[EvalResult]) -> float | None:
        """Score a complete, successful cohort only; never carry stale scores into it."""
        names = self.selection_names()
        rows = {r.name: r for r in results if r.name in names}
        if not names or set(rows) != names:
            return None
        if any(r.status != "ok" or r.score is None for r in rows.values()):
            return None
        return sum(float(r.score) for r in rows.values()) / len(rows)

    @staticmethod
    def no_goal() -> "GoalConfig":
        return GoalConfig(goals=())

    @staticmethod
    def single(goal: Goal, *, selection: str = "none") -> "GoalConfig":
        return GoalConfig(goals=(goal,), selection=selection)

    @staticmethod
    def multi(goals: Sequence[Goal], *, selection: str = "none") -> "GoalConfig":
        return GoalConfig(goals=tuple(goals), selection=selection)

    @staticmethod
    def of(text: str = "", evaluators: Sequence[EvaluatorSpec] = (),
           *, selection: str = "none") -> "GoalConfig":
        """The decoupled form: freeform objective text + independent evaluators."""
        return GoalConfig(text=text, evaluators=tuple(evaluators), selection=selection)

    @property
    def is_no_goal(self) -> bool:
        """No announced objective. Evaluators may still run — measured but unstated is a
        real condition (G-blind); unmeasured but stated is another (an aim on trust)."""
        return not self.goals and not self.text

    def goal_text(self) -> str:
        """Objective text to show the agent in every phase (empty under no-goal)."""
        stated = ([self.text] if self.text else []) + [g.text for g in self.goals if g.text]
        if not stated:
            return ""
        if len(stated) == 1:
            return stated[0]
        return "You are pursuing several objectives at once:\n" + "\n".join(
            f"  {i+1}. {t}" for i, t in enumerate(stated))

    def evaluate(self, trace: Sequence[ActionEvent], ctx: GoalContext) -> list[EvalResult]:
        """Run due evaluators independently; one broken signal never erases the others."""
        out: list[EvalResult] = []

        def isolated(name: str, evaluator: Evaluator, error_policy: str = "zero") -> EvalResult:
            try:
                result = evaluator(trace, ctx)
                if result.status not in ("ok", "error", "missing"):
                    raise ValueError(f"unknown result status {result.status!r}")
                if result.score is None and (result.status == "ok" or result.passed):
                    raise ValueError("a missing score cannot be successful")
                if result.score is not None and not math.isfinite(float(result.score)):
                    raise ValueError(f"non-finite score {result.score!r}")
                if result.status != "ok" and result.passed:
                    raise ValueError("an unavailable evaluation cannot pass")
                from proteus.core.continuity import _redact
                return replace(result, name=name,
                               score=float(result.score) if result.score is not None else None,
                               detail=_redact(str(result.detail))[:8000])
            except Exception as exc:  # noqa: BLE001 - a broken evaluator is one result
                from proteus.core.continuity import _redact
                return EvalResult(
                    name=name, score=None if error_policy == "missing" else 0.0, passed=False,
                    detail=_redact(f"evaluator error: {type(exc).__name__}: {exc}")[:2000],
                    status="error",
                )

        for g in self.goals:
            if g.evaluator is not None and ctx.episode > 0:
                out.append(isolated(g.name, g.evaluator))
        for spec in self.evaluators:
            if spec.due(ctx.episode):
                out.append(isolated(spec.name, spec.run, spec.error_policy))
        return out

    def _visible(self) -> list[tuple[str, str]]:
        """(name, label) of every OBSERVE-visible signal."""
        vis = [(g.name, g.name) for g in self.goals if g.visibility is Visibility.OBSERVE]
        vis += [(s.name, s.name) for s in self.evaluators
                if s.visibility is Visibility.OBSERVE]
        return vis

    def observe_feedback(self, results: Mapping[str, EvalResult]) -> str:
        """The text shown to the agent in the next observe phase, for OBSERVE-visible
        signals only. HIDDEN-visibility scores never appear here."""
        lines = []
        for name, label in self._visible():
            if name in results:
                r = results[name]
                score = f"score {r.score:.3f}" if r.score is not None else "score unavailable"
                if r.status != "ok":
                    score = "score unavailable"
                lines.append(f"- {label}: {score} (status: {r.status})"
                             + (f" — {r.detail}" if r.detail else ""))
        if not lines:
            return ""
        return ("Feedback on your last episode from the evaluators you can see:\n"
                + "\n".join(lines))

    def describe(self) -> list[dict]:
        """Evaluator manifest rows (name, kind, visibility) for the run's records."""
        rows = [{"name": g.name, "kind": "goal", "visibility": g.visibility.value}
                for g in self.goals if g.evaluator is not None]
        rows += [{"name": s.name, "kind": s.kind, "visibility": s.visibility.value}
                 for s in self.evaluators]
        # Keep legacy default manifests byte-compatible. Non-default policy is explicit.
        specs = {g.name: g for g in self.goals if g.evaluator is not None}
        specs.update({s.name: s for s in self.evaluators})
        for row in rows:
            spec = specs[row["name"]]
            if not spec.selection_eligible:
                row["selection_eligible"] = False
            if isinstance(spec, EvaluatorSpec):
                if spec.episodes is not None:
                    row["episodes"] = list(spec.episodes)
                elif spec.every_n_episodes != 1 or spec.include_initial:
                    row.update(every_n_episodes=spec.every_n_episodes,
                               include_initial=spec.include_initial)
                if spec.error_policy != "zero":
                    row["error_policy"] = spec.error_policy
        return rows
