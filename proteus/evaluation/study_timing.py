"""End-to-end study elapsed time, separate from summed operation durations."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import time
import uuid

from proteus.artifacts.store import publish_bytes
from proteus.core.contracts import DIGEST, NONNEGATIVE, TEXT, require
from proteus.core.serialization import Contract, content_hash


@dataclass(frozen=True, kw_only=True)
class StudyStart(Contract):
    config_hash: str = field(metadata=DIGEST)
    lock_hash: str = field(metadata=DIGEST)
    invocation: str = field(metadata=TEXT)
    started_at: str = field(metadata=TEXT)


@dataclass(frozen=True, kw_only=True)
class StudyElapsed(Contract):
    start_hash: str = field(metadata=DIGEST)
    wall_seconds: float | None = field(metadata=NONNEGATIVE)
    interrupted: bool

    def _validate(self):
        require(self.interrupted == (self.wall_seconds is None), "interrupted study elapsed time must stay unknown")


def read(directory, config_hash, lock_hash):
    directory = Path(directory)
    start = StudyStart.from_json((directory / "start.json").read_text())
    require((start.config_hash, start.lock_hash) == (config_hash, lock_hash), "study timing identity differs")
    elapsed = StudyElapsed.from_json((directory / "elapsed.json").read_text())
    require(elapsed.start_hash == content_hash(start), "study elapsed time belongs to another start")
    return elapsed


def measure(directory, config_hash, lock_hash, action):
    """Caller holds the study lock; action returns only after a completed run.

    Recovered finished runs still execute their normal resume validation. A start
    left by a stopped process cannot establish the complete elapsed duration.
    """
    directory = Path(directory)
    if (directory / "elapsed.json").exists():
        elapsed = read(directory, config_hash, lock_hash)
        action()
        return elapsed
    beginning = time.monotonic()
    continuous = not (directory / "start.json").exists()
    if continuous:
        start = StudyStart(config_hash=config_hash, lock_hash=lock_hash,
                           invocation=uuid.uuid4().hex, started_at=datetime.now(timezone.utc).isoformat())
        publish_bytes(directory / "start.json", (start.to_json() + "\n").encode())
    else:
        start = StudyStart.from_json((directory / "start.json").read_text())
        require((start.config_hash, start.lock_hash) == (config_hash, lock_hash), "study timing identity differs")
    action()
    elapsed = StudyElapsed(start_hash=content_hash(start),
        wall_seconds=time.monotonic() - beginning if continuous else None, interrupted=not continuous)
    publish_bytes(directory / "elapsed.json", (elapsed.to_json() + "\n").encode())
    return elapsed
