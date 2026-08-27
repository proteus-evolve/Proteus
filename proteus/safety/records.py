"""Records and contexts for settled-episode safety execution."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol, runtime_checkable

from proteus.safety.taxonomy import SafetyStatus


@dataclass(frozen=True)
class SettledSnapshotRef:
    run_id: str
    episode: int

    def to_dict(self) -> dict[str, object]:
        return {"run_id": self.run_id, "episode": self.episode}


class SafetyExecutionStatus(str, Enum):
    EVALUATED = "evaluated"
    NOT_SCHEDULED = "not_scheduled"
    NOT_EVALUATED = "not_evaluated"
    ERROR = "error"


@dataclass(frozen=True)
class SettledEpisodeSafetyContext:
    run_id: str
    episode: int
    episodes_target: int
    snapshot: SettledSnapshotRef
    checkpoint_commit: str
    source_harness_root: Path
    goal_text: str
    controller_root: Path


@dataclass(frozen=True)
class FamilyEvaluationContext:
    settled: SettledEpisodeSafetyContext
    snapshot_root: Path
    trial_root: Path
    artifact_root: Path
    evidence_dir: Path
    probe_seed: str = ""

    @property
    def run_id(self) -> str:
        return self.settled.run_id

    @property
    def episode(self) -> int:
        return self.settled.episode

    @property
    def snapshot(self) -> SettledSnapshotRef:
        return self.settled.snapshot

    @property
    def goal_text(self) -> str:
        return self.settled.goal_text


@runtime_checkable
class SafetyFamilyObservation(Protocol):
    family_id: str
    family_version: str
    snapshot: SettledSnapshotRef
    evidence_complete: bool
    terminal_status: SafetyStatus
    live_calls: int
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True)
class FamilyExecutionRecord:
    family_id: str
    family_version: str
    episode: int
    execution_status: SafetyExecutionStatus
    observation_ref: str = ""
    delta_ref: str = ""
    last_observed_episode: int | None = None
    reason: str = ""
    live_calls: int = 0
    wall_time_s: float = 0.0

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "FamilyExecutionRecord":
        values = dict(payload)
        values["execution_status"] = SafetyExecutionStatus(str(values["execution_status"]))
        return cls(**values)

    @classmethod
    def not_scheduled(
        cls,
        *,
        family_id: str,
        family_version: str,
        episode: int,
        last_observed_episode: int | None,
    ) -> "FamilyExecutionRecord":
        return cls(
            family_id=family_id,
            family_version=family_version,
            episode=episode,
            execution_status=SafetyExecutionStatus.NOT_SCHEDULED,
            last_observed_episode=last_observed_episode,
            reason="family_not_scheduled",
        )


@dataclass(frozen=True)
class EpisodeSafetyRecord:
    run_id: str
    episode: int
    snapshot: SettledSnapshotRef
    checkpoint_commit: str
    families: tuple[FamilyExecutionRecord, ...]
    complete: bool
    artifact_ref: str

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "EpisodeSafetyRecord":
        return cls(
            run_id=str(payload["run_id"]),
            episode=int(payload["episode"]),
            snapshot=SettledSnapshotRef(**payload["snapshot"]),
            checkpoint_commit=str(payload["checkpoint_commit"]),
            families=tuple(
                FamilyExecutionRecord.from_dict(item) for item in payload["families"]
            ),
            complete=bool(payload["complete"]),
            artifact_ref=str(payload["artifact_ref"]),
        )
