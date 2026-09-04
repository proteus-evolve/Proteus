"""Durable, append-only observations for settled-episode safety."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from proteus.safety.records import (
    EpisodeSafetyRecord,
    FamilyExecutionRecord,
    SafetyExecutionStatus,
)


@dataclass(frozen=True)
class FamilyHistoryEntry:
    """One evaluated family observation and the checkpoint it measured."""

    episode: int
    checkpoint_commit: str
    record: FamilyExecutionRecord


class SafetyHistoryStore:
    """Persist ordered episode records and one separately stored family baseline."""

    def __init__(self, controller_root: Path, run_id: str) -> None:
        self.root = Path(controller_root) / "safety-episodes" / run_id
        self.path = self.root / "history.jsonl"
        self.baseline_root = self.root / "episode-000" / "families"

    def records(self) -> tuple[EpisodeSafetyRecord, ...]:
        if not self.path.is_file():
            return ()
        return tuple(
            EpisodeSafetyRecord.from_dict(json.loads(line))
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )

    def record_for_episode(self, episode: int) -> EpisodeSafetyRecord | None:
        return next((record for record in self.records() if record.episode == episode), None)

    def published_record(self, episode: int) -> EpisodeSafetyRecord | None:
        path = self.root / f"episode-{episode:03d}" / "summary.json"
        if not path.is_file():
            return None
        return EpisodeSafetyRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def append(self, record: EpisodeSafetyRecord) -> None:
        existing = self.records()
        expected = len(existing) + 1
        if record.episode != expected:
            raise ValueError(f"safety history expected episode {expected}, got {record.episode}")

        self.root.mkdir(parents=True, exist_ok=True)
        payload = asdict(record)
        payload["snapshot"] = record.snapshot.to_dict()
        payload["families"] = [
            {**asdict(family), "execution_status": family.execution_status.value}
            for family in record.families
        ]
        previous = self.path.read_text(encoding="utf-8") if self.path.is_file() else ""
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(previous + json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(self.path)

    def last_observed(self, family_id: str) -> FamilyHistoryEntry | None:
        for episode in reversed(self.records()):
            for family in episode.families:
                if (
                    family.family_id == family_id
                    and family.execution_status is SafetyExecutionStatus.EVALUATED
                ):
                    return FamilyHistoryEntry(
                        episode=episode.episode,
                        checkpoint_commit=episode.checkpoint_commit,
                        record=family,
                    )
        return None

    def write_baseline(self, entry: FamilyHistoryEntry) -> None:
        if entry.episode != 0 or entry.record.episode != 0:
            raise ValueError("safety baseline must use episode zero")
        path = self.baseline_root / entry.record.family_id / "index.json"
        if path.exists():
            raise ValueError(f"safety baseline already exists for {entry.record.family_id}")
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "episode": entry.episode,
            "checkpoint_commit": entry.checkpoint_commit,
            "record": {
                **asdict(entry.record),
                "execution_status": entry.record.execution_status.value,
            },
        }
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(
            json.dumps(payload, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)

    def published_baseline(
        self,
        family_id: str,
        *,
        checkpoint_commit: str,
    ) -> FamilyHistoryEntry | None:
        path = self.baseline_root / family_id / "execution.json"
        if not path.is_file():
            return None
        record = FamilyExecutionRecord.from_dict(
            json.loads(path.read_text(encoding="utf-8"))
        )
        if record.episode != 0 or record.family_id != family_id:
            raise ValueError("published safety baseline has mismatched identity")
        return FamilyHistoryEntry(
            episode=0,
            checkpoint_commit=checkpoint_commit,
            record=record,
        )

    def baseline(self, family_id: str) -> FamilyHistoryEntry | None:
        path = self.baseline_root / family_id / "index.json"
        if not path.is_file():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        return FamilyHistoryEntry(
            episode=int(payload["episode"]),
            checkpoint_commit=str(payload["checkpoint_commit"]),
            record=FamilyExecutionRecord.from_dict(payload["record"]),
        )
