"""Opt-in native phase continuation from exact durable evidence, never implicit resume."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from proteus.core.budget import PHASES, validate_phases


@dataclass(frozen=True)
class DshPhaseResume:
    root: Path
    episode: int
    phase: str
    mapping: dict[str, list[str]]
    trace_bytes: bytes
    session_sha256: dict[str, str]
    phase_calls: dict[str, int]
    handoff_attempt: int
    handoff_sha256: str
    checkpoint_misses: int
    timeout_s: int
    phases: tuple[str, ...] = PHASES

    @classmethod
    def capture(cls, root: Path, episode: int, phase: str, *, timeout_s: int,
                phases=PHASES):
        """Caller must also validate checkpoint/goal/runtime and own an exclusive run lock."""
        from proteus.adapters.dsh import _zstd_partial

        phase_names = validate_phases(phases)
        if (phase not in phase_names or not isinstance(episode, int) or isinstance(episode, bool)
                or episode < 1 or not isinstance(timeout_s, int)
                or isinstance(timeout_s, bool) or timeout_s < 1):
            raise ValueError("invalid phase recovery identity/deadline")
        root = Path(root).resolve()
        trace_bytes = (root / "traces" / f"ep{episode:03d}.json").read_bytes()
        mapping = json.loads(trace_bytes)
        prefix = phase_names[:phase_names.index(phase) + 1]
        if not isinstance(mapping, dict) or set(mapping) != set(prefix):
            raise ValueError("recovery requires exactly the observed phase prefix")
        state = root / ".dsh-state"
        hashes, counts, seen = {}, {}, set()
        for name in prefix:
            values = mapping[name]
            if not isinstance(values, list) or not values:
                raise ValueError("each recovered phase requires native sessions")
            counts[name] = 0
            for relative in values:
                if not isinstance(relative, str):
                    raise ValueError("invalid session path")
                rel = Path(relative)
                if rel.is_absolute() or ".." in rel.parts or not rel.parts or rel.parts[0] != "sessions":
                    raise ValueError("session must be inside native state")
                path = state / rel / "session.jsonl.zstd"
                if (relative in seen or not path.is_file()
                        or any(part.is_symlink() for part in (path, *path.parents))
                        or not path.resolve().is_relative_to(state.resolve())):
                    raise ValueError("duplicate, missing, or linked native session")
                seen.add(relative)
                payload = path.read_bytes()
                hashes[relative] = hashlib.sha256(payload).hexdigest()
                # Same native marker count used by the existing live budget.
                decoded = _zstd_partial(payload)
                if not decoded.strip():
                    raise ValueError("native session has no decodable recovery evidence")
                counts[name] += decoded.count(b'"tool/call"')
        history = root / ".proteus-state/handoffs" / f"ep{episode:03d}"
        records = []
        for path in history.glob("*.json"):
            record = json.loads(path.read_text())
            if record.get("episode") == episode and record.get("phase") in prefix:
                records.append((path, record))
        candidates = [(path, row) for path, row in records if row["phase"] == phase]
        if not candidates:
            raise ValueError("missing interrupted-phase handoff")
        chosen, record = max(candidates, key=lambda pair: pair[1].get("attempt", 0))
        attempt = record.get("attempt")
        if type(attempt) is not int or attempt < 1:
            raise ValueError("invalid handoff attempt")
        expected = phase if attempt == 1 else f"{phase}-{attempt:02d}"
        if chosen.name != expected + ".json" or not record.get("interrupted"):
            raise ValueError("recovery requires the interrupted handoff attempt")
        return cls(root, episode, phase, {name: list(mapping[name]) for name in prefix},
            trace_bytes, hashes, counts, attempt, hashlib.sha256(chosen.read_bytes()).hexdigest(),
            sum(row.get("source") != "agent" for _, row in records), timeout_s,
            phase_names)

    @property
    def used_calls(self) -> int:
        return sum(self.phase_calls.values())

    @property
    def phase_start_calls(self) -> int:
        return sum(self.phase_calls[name] for name in self.phases[:self.phases.index(self.phase)])

    def verify(self, root: Path, episode: int, *, original_trace: bool = False) -> None:
        if Path(root).resolve() != self.root or episode != self.episode:
            raise ValueError("phase recovery belongs to a different run/episode")
        for relative, expected in self.session_sha256.items():
            path = self.root / ".dsh-state" / relative / "session.jsonl.zstd"
            if any(part.is_symlink() for part in (path, *path.parents)):
                raise ValueError("recovery evidence became a symlink")
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError("prior native evidence changed")
        stem = self.phase if self.handoff_attempt == 1 else f"{self.phase}-{self.handoff_attempt:02d}"
        path = self.root / ".proteus-state/handoffs" / f"ep{episode:03d}" / (stem + ".json")
        if hashlib.sha256(path.read_bytes()).hexdigest() != self.handoff_sha256:
            raise ValueError("prior handoff evidence changed")
        if original_trace and (self.root / "traces" / f"ep{episode:03d}.json").read_bytes() != self.trace_bytes:
            raise ValueError("native phase mapping changed before recovery")

    def preserve_trace(self) -> None:
        digest = hashlib.sha256(self.trace_bytes).hexdigest()
        path = self.root / "traces" / f"ep{self.episode:03d}.attempt-{digest}.json"
        if path.exists():
            if path.read_bytes() != self.trace_bytes:
                raise ValueError("attempt trace identity collision")
        else:
            with path.open("xb") as handle:
                handle.write(self.trace_bytes)

    def describe(self) -> dict:
        return {"episode": self.episode, "restart_phase": self.phase,
            "skipped_phases": list(self.phases[:self.phases.index(self.phase)]),
            "session_sha256": self.session_sha256, "phase_calls": self.phase_calls,
            "used_calls": self.used_calls, "phase_start_calls": self.phase_start_calls,
            "trace_sha256": hashlib.sha256(self.trace_bytes).hexdigest(),
            "handoff_attempt": self.handoff_attempt, "handoff_sha256": self.handoff_sha256,
            "prior_checkpoint_misses": self.checkpoint_misses,
            "recovered_phase_timeout_seconds": self.timeout_s}
