"""Harness-neutral episode budget planning.

The core records one budget condition and adapters consume the resulting ``BudgetPlan``.
This keeps allocation policy out of individual harness integrations while leaving the
adapter responsible for counting and stopping its own native tool loop.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


PHASES = ("observe", "propose", "act", "reflect")
BUDGET_PROTOCOL_VERSION = 1


def _integer(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer, got {value!r}")
    return value


@dataclass(frozen=True)
class BudgetPlan:
    """Validated allocation and hard-stop policy for one episode.

    ``normal_limit`` is the planned budget. ``hard_limit`` is a burst ceiling. With an
    explicit phase allocation, observe and propose receive only their planned allowance;
    anything they leave unused, plus the burst allowance, is made available to act. The
    later phases retain their planned reserve. This makes act the only phase that can
    deliberately borrow from the pool while preserving a bounded reflect phase.
    """

    normal_limit: int
    hard_limit: int
    phase_allowances: Mapping[str, int]
    min_turns_per_phase: int = 0
    checkpoint_turns: int = 0

    @property
    def enabled(self) -> bool:
        return self.hard_limit > 0

    @property
    def explicit(self) -> bool:
        return bool(self.phase_allowances)

    def planned_allowance(self, phase: str) -> int:
        """Return the phase's normal-plan allowance, or its legacy reserve."""
        self._phase_index(phase)
        if self.explicit:
            return int(self.phase_allowances[phase])
        return self.min_turns_per_phase

    def stop_at(self, phase: str, used_before: int) -> int:
        """Cumulative call count at which an adapter must stop this phase.

        A zero return means unbounded. For the explicit protocol, pre-act phases cannot
        spend each other's unused quota. Act receives the unused quota and the difference
        between the normal and hard limits. Every other phase stays bounded by its own
        planned allowance.
        """
        idx = self._phase_index(phase)
        used_before = max(0, int(used_before))
        if not self.enabled:
            return 0
        if not self.explicit:
            later = len(PHASES) - idx - 1
            return self.hard_limit - self.min_turns_per_phase * later

        if phase != "act":
            return min(self.hard_limit, used_before + self.planned_allowance(phase))
        later_reserve = sum(
            int(self.phase_allowances[name]) for name in PHASES[idx + 1:]
        )
        return max(used_before, self.hard_limit - later_reserve)

    def prompt(self, phase: str, used_before: int, episode: int,
               continuity_mode: str = "native") -> str:
        """Render the live phase-start budget contract shown to the subject agent."""
        stop = self.stop_at(phase, used_before)
        if not stop:
            return ""
        used = max(0, int(used_before))
        phase_remaining = max(0, stop - used)
        hard_remaining = max(0, self.hard_limit - used)
        lines = [
            f"Proteus budget protocol v{BUDGET_PROTOCOL_VERSION} — episode {episode}, "
            f"phase {phase}:",
            f"- calls already used before this phase: {used}",
            f"- normal episode budget: {self.normal_limit}",
            f"- episode hard ceiling: {self.hard_limit} ({hard_remaining} remain)",
            f"- this phase stops at cumulative call {stop} "
            f"({phase_remaining} calls available now)",
        ]
        if self.explicit:
            lines.insert(4, f"- planned allowance for this phase: "
                            f"{self.planned_allowance(phase)}")
        elif self.min_turns_per_phase:
            lines.insert(4, f"- configured reserve floor per phase: "
                            f"{self.min_turns_per_phase}")
        else:
            lines.insert(4, "- no phase allocation: this phase shares the remaining cap")
        if self.explicit and phase == "act":
            burst = self.hard_limit - self.normal_limit
            lines.append(
                "- act owns unused earlier-phase allowance"
                + (f" and up to {burst} burst calls" if burst else "")
            )
        if self.checkpoint_turns:
            checkpoint_at = max(used, stop - self.checkpoint_turns)
            lines.append(
                f"- checkpoint reserve: keep final {self.checkpoint_turns} calls unspent; "
                "before ending (even early), use them to persist a concise continuation "
                f"checkpoint; cumulative call {checkpoint_at} is the latest ordinary-work "
                "boundary"
            )
            if continuity_mode == "framework":
                lines.append(
                    "- checkpoint target: /workspace/.proteus/handoff.md; Proteus will "
                    "archive what you write but will not invent a semantic summary for you"
                )
        return "\n".join(lines)

    @staticmethod
    def _phase_index(phase: str) -> int:
        try:
            return PHASES.index(phase)
        except ValueError as exc:
            raise ValueError(f"unknown episode phase {phase!r}") from exc


def make_budget_plan(*, max_turns: int, min_turns_per_phase: int = 0,
                     phase_turns: Mapping[str, int] | None = None,
                     hard_max_turns: int = 0, checkpoint_turns: int = 0) -> BudgetPlan:
    """Validate public budget knobs and return their canonical execution plan."""
    normal = _integer("max_turns", max_turns)
    minimum = _integer("min_turns_per_phase", min_turns_per_phase)
    hard_arg = _integer("hard_max_turns", hard_max_turns)
    checkpoint = _integer("checkpoint_turns", checkpoint_turns)
    if normal < 0:
        raise ValueError("max_turns must be 0 (unlimited) or a positive integer")
    if minimum < 0:
        raise ValueError("min_turns_per_phase must be 0 or a positive integer")
    if hard_arg < 0:
        raise ValueError("hard_max_turns must be 0 or a positive integer")
    if checkpoint < 0:
        raise ValueError("checkpoint_turns must be 0 or a positive integer")

    raw = dict(phase_turns or {})
    if raw:
        missing = [phase for phase in PHASES if phase not in raw]
        extra = sorted(repr(key) for key in raw if key not in PHASES)
        if missing or extra:
            detail = []
            if missing:
                detail.append(f"missing {', '.join(missing)}")
            if extra:
                detail.append(f"unknown {', '.join(extra)}")
            raise ValueError("phase_turns must name observe, propose, act, reflect (" +
                             "; ".join(detail) + ")")
        canonical = {phase: _integer(f"phase_turns[{phase}]", raw[phase])
                     for phase in PHASES}
        if any(value < 0 for value in canonical.values()):
            raise ValueError("phase_turns values must be 0 or positive integers")
        if not normal:
            raise ValueError("phase_turns requires a positive max_turns normal budget")
        if sum(canonical.values()) != normal:
            raise ValueError(
                f"phase_turns sum to {sum(canonical.values())}, not max_turns={normal}"
            )
        if minimum:
            raise ValueError("phase_turns cannot be combined with min_turns_per_phase")
        hard = hard_arg or normal
        if hard < normal:
            raise ValueError(
                f"hard_max_turns={hard} must be at least max_turns={normal}"
            )
        if checkpoint and checkpoint > min(canonical.values()):
            raise ValueError(
                "checkpoint_turns cannot exceed the smallest phase_turns allowance"
            )
        return BudgetPlan(normal, hard, canonical, checkpoint_turns=checkpoint)

    if hard_arg:
        raise ValueError("hard_max_turns requires an explicit phase_turns plan")
    if checkpoint:
        raise ValueError("checkpoint_turns requires an explicit phase_turns plan")
    if normal and minimum * len(PHASES) > normal:
        raise ValueError(
            f"max_turns={normal} cannot honour min_turns_per_phase={minimum}: "
            f"{len(PHASES)} phases need at least {minimum * len(PHASES)} turns"
        )
    return BudgetPlan(normal, normal, {}, min_turns_per_phase=minimum)


def budget_plan(spec: object) -> BudgetPlan:
    """Build a plan from an EpisodeSpec-like object (keeps custom adapters simple)."""
    return make_budget_plan(
        max_turns=getattr(spec, "max_turns", 0) or 0,
        min_turns_per_phase=getattr(spec, "min_turns_per_phase", 0) or 0,
        phase_turns=getattr(spec, "phase_turns", None),
        hard_max_turns=getattr(spec, "hard_max_turns", 0) or 0,
        checkpoint_turns=getattr(spec, "checkpoint_turns", 0) or 0,
    )


def phase_prompt(spec: object, phase: str, used_before: int) -> str:
    """Add a live budget header when this experimental condition announces it."""
    prompt = str(getattr(spec, "phase_prompts", {}).get(phase, ""))
    if not bool(getattr(spec, "announce_budget", False)):
        return prompt
    note = budget_plan(spec).prompt(
        phase, used_before, int(getattr(spec, "episode", 0) or 0),
        str(getattr(spec, "continuity_mode", "native")),
    )
    return f"{note}\n\n{prompt}" if note else prompt
