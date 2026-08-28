"""Explicit schedules for settled-episode safety family evaluations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@runtime_checkable
class FamilySchedule(Protocol):
    def selected(self, *, episode: int, episodes_target: int) -> bool: ...


@dataclass(frozen=True)
class EveryEpisodeSchedule:
    def selected(self, *, episode: int, episodes_target: int) -> bool:
        return 1 <= episode <= episodes_target


@dataclass(frozen=True)
class EveryNEpisodesSchedule:
    step: int
    include_first: bool = True
    include_final: bool = True

    def __post_init__(self) -> None:
        if type(self.step) is not int or self.step < 1:
            raise ValueError("family schedule step must be positive")

    def selected(self, *, episode: int, episodes_target: int) -> bool:
        if episode < 1 or episode > episodes_target:
            return False
        return (
            (self.include_first and episode == 1)
            or episode % self.step == 0
            or (self.include_final and episode == episodes_target)
        )


@dataclass(frozen=True)
class ExplicitEpisodesSchedule:
    episodes: frozenset[int]

    def __post_init__(self) -> None:
        if any(type(episode) is not int or episode < 1 for episode in self.episodes):
            raise ValueError("explicit scheduled episodes must be positive integers")

    def selected(self, *, episode: int, episodes_target: int) -> bool:
        return episode in self.episodes and episode <= episodes_target


def parse_family_schedule(spec: str, episodes_target: int) -> FamilySchedule:
    """Parse ``every``, ``every:N``, explicit episodes, ``last``, or ``final``."""
    if type(episodes_target) is not int or episodes_target < 1:
        raise ValueError("family schedule requires a positive episode target")
    if not isinstance(spec, str):
        raise TypeError("family schedule must be a string")
    value = spec.strip().lower()
    if value == "every":
        return EveryEpisodeSchedule()
    if value == "final":
        return ExplicitEpisodesSchedule(frozenset({episodes_target}))
    if value.startswith("every:") and "," not in value:
        try:
            step = int(value.split(":", 1)[1])
        except ValueError as exc:
            raise ValueError(f"invalid family schedule token: {value!r}") from exc
        return EveryNEpisodesSchedule(step=step)

    selected: set[int] = set()
    for raw_token in value.split(","):
        token = raw_token.strip()
        if token == "last":
            selected.add(episodes_target)
            continue
        if token.startswith("every:"):
            raw_step = token.split(":", 1)[1]
            try:
                step = int(raw_step)
            except ValueError as exc:
                raise ValueError(f"invalid family schedule token: {token!r}") from exc
            if step < 1:
                raise ValueError("family schedule step must be positive")
            selected.add(1)
            selected.update(range(step, episodes_target + 1, step))
            selected.add(episodes_target)
            continue
        if token:
            try:
                episode = int(token)
            except ValueError as exc:
                raise ValueError(f"invalid family schedule token: {token!r}") from exc
            if episode < 1 or episode > episodes_target:
                raise ValueError(
                    f"scheduled episode {episode} is outside 1..{episodes_target}"
                )
            selected.add(episode)
    if not selected:
        raise ValueError("family schedule is empty")
    return ExplicitEpisodesSchedule(frozenset(selected))


def parse_collapse_episodes(spec: str, episodes: int) -> frozenset[int]:
    """Parse the collapse-family selection syntax used by the CLI."""
    if episodes < 1:
        raise ValueError("collapse episode selection requires a positive episode count")
    selected: set[int] = set()
    for raw in spec.split(","):
        token = raw.strip().lower()
        if not token:
            continue
        if token == "last":
            selected.add(episodes)
            continue
        if token.startswith("every:"):
            try:
                step = int(token.split(":", 1)[1])
            except ValueError as exc:
                raise ValueError(
                    f"collapse episode {raw!r} must be every:<positive integer>"
                ) from exc
            if step < 1:
                raise ValueError("collapse every-N step must be a positive integer")
            selected.add(1)
            selected.update(range(step, episodes + 1, step))
            continue
        try:
            value = int(token)
        except ValueError as exc:
            raise ValueError(
                f"collapse episode {raw!r} must be an integer, 'last', or 'every:N'"
            ) from exc
        if value < 1 or value > episodes:
            raise ValueError(f"collapse episode {value} is outside 1..{episodes}")
        selected.add(value)
    if not selected:
        raise ValueError("collapse episode selection is empty")
    return frozenset(selected)


# Transitional names are retained for the still-live gate/CLI only. They carry
# no snapshot roles or endpoint semantics and delegate to the settled contracts.
class EveryEpisode(EveryEpisodeSchedule):
    def should_run(self, episode: int, episodes_target: int) -> bool:
        return self.selected(episode=episode, episodes_target=episodes_target)


@dataclass(frozen=True)
class EveryN(EveryNEpisodesSchedule):
    n: int = 5

    def __init__(self, n: int = 5) -> None:
        object.__setattr__(self, "n", n)
        object.__setattr__(self, "step", n)
        object.__setattr__(self, "include_first", True)
        object.__setattr__(self, "include_final", False)
        EveryNEpisodesSchedule.__post_init__(self)

    def should_run(self, episode: int, episodes_target: int) -> bool:
        return self.selected(episode=episode, episodes_target=episodes_target)


@dataclass(frozen=True)
class ExplicitEpisodes(ExplicitEpisodesSchedule):
    def should_run(self, episode: int, episodes_target: int) -> bool:
        return self.selected(episode=episode, episodes_target=episodes_target)


@dataclass(frozen=True)
class SafetySuiteSchedule:
    memory_bad_admission: FamilySchedule = field(default_factory=EveryEpisode)
    memory_collapse: FamilySchedule = field(default_factory=lambda: EveryN(5))
    tools_permission_drift: FamilySchedule = field(default_factory=EveryEpisode)

    def for_family(self, family_id: str) -> FamilySchedule:
        try:
            return {
                "memory_bad_admission": self.memory_bad_admission,
                "memory_collapse": self.memory_collapse,
                "tools_permission_drift": self.tools_permission_drift,
            }[family_id]
        except KeyError as exc:
            raise ValueError(f"no schedule for safety family {family_id}") from exc


DEFAULT_PHASE1_SCHEDULE = SafetySuiteSchedule()
