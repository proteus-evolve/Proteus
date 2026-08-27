from dataclasses import dataclass
from pathlib import Path

from proteus.core import snapshot
from proteus.safety.taxonomy import SafetyStatus


@dataclass(frozen=True)
class FakeObservation:
    family_id: str
    family_version: str
    snapshot: object
    evidence_complete: bool
    terminal_status: SafetyStatus
    live_calls: int
    evidence_refs: tuple[str, ...]


class FakePlugin:
    family_version = "3"
    live_call_cap = 0

    def __init__(self, family_id: str, *, fail: bool = False) -> None:
        self.family_id = family_id
        self.fail = fail
        self.calls: list[int] = []

    def evaluate(self, context):
        self.calls.append(context.settled.episode)
        if self.fail:
            raise RuntimeError("family boom")
        evidence = context.evidence_dir / "evidence.json"
        evidence.parent.mkdir(parents=True, exist_ok=True)
        evidence.write_text("{}\n", encoding="utf-8")
        return FakeObservation(
            family_id=self.family_id,
            family_version=self.family_version,
            snapshot=context.settled.snapshot,
            evidence_complete=True,
            terminal_status=SafetyStatus.PASS,
            live_calls=0,
            evidence_refs=(evidence.relative_to(context.artifact_root).as_posix(),),
        )

    def compare(self, previous, baseline, current):
        del previous, baseline, current
        return {"change": "unavailable"}

    def load(self, path: Path):
        import json

        from proteus.safety.records import SettledSnapshotRef

        payload = json.loads(path.read_text(encoding="utf-8"))
        return FakeObservation(
            family_id=payload["family_id"],
            family_version=payload["family_version"],
            snapshot=SettledSnapshotRef(**payload["snapshot"]),
            evidence_complete=bool(payload["evidence_complete"]),
            terminal_status=SafetyStatus(payload["terminal_status"]),
            live_calls=int(payload["live_calls"]),
            evidence_refs=tuple(payload["evidence_refs"]),
        )


def seeded_run(tmp_path: Path):
    harness = tmp_path / "run" / "harness"
    harness.mkdir(parents=True)
    (harness / "state.txt").write_text("state\n", encoding="utf-8")
    snapshot.init(harness)
    return harness, snapshot.head(harness)
