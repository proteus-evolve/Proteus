"""Persistent controller-owned challenge registrations for settled safety trials."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

ADVBENCH_PANEL_SIZE = 16
EXACT_ADVBENCH_SLOTS = 8
NEAR_NAME_RULES = (
    "append_plural_s",
    "repeat_last_character",
    "transpose_last_two_characters",
)
DEFAULT_ADMISSION_BEHAVIOR_SCHEDULE = "1,every:5,last"
DEFAULT_PRESSURE_LEVELS = (0, 2_000, 8_000, 32_000, 64_000)
_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class RecordChallenge:
    state_id: str
    exact_advbench_index: int
    shadow_advbench_index: int
    near_name_rule: str
    near_name: str
    lookup_query: str


class ChallengeManifest:
    """An append-only record mapping with immutable run-level challenge inputs."""

    def __init__(
        self,
        *,
        path: Path,
        advbench_indices: tuple[int, ...],
        record_challenges: dict[str, RecordChallenge],
    ) -> None:
        self.path = path
        self.advbench_indices = advbench_indices
        self._record_challenges = record_challenges

    @property
    def record_challenges(self) -> tuple[RecordChallenge, ...]:
        return tuple(self._record_challenges.values())

    def challenge_for(self, state_id: str) -> RecordChallenge | None:
        return self._record_challenges.get(state_id)

    def register_record(
        self,
        state_id: str,
        lookup_query: str,
        *,
        occupied_state_ids: tuple[str, ...] = (),
    ) -> RecordChallenge:
        """Persist the first deterministic mapping for a newly eligible memory record."""
        _validate_state_id(state_id)
        if not isinstance(lookup_query, str) or not lookup_query.strip():
            raise ValueError("memory lookup query must be non-empty text")
        existing = self.challenge_for(state_id)
        if existing is not None:
            return existing

        ordinal = len(self._record_challenges)
        exact_slot = ordinal % EXACT_ADVBENCH_SLOTS
        rule_index = ordinal % len(NEAR_NAME_RULES)
        near_name, near_name_rule = _available_near_name(
            state_id,
            starting_rule_index=rule_index,
            occupied=set(occupied_state_ids)
            | set(self._record_challenges)
            | {item.near_name for item in self._record_challenges.values()},
        )
        challenge = RecordChallenge(
            state_id=state_id,
            exact_advbench_index=self.advbench_indices[exact_slot],
            shadow_advbench_index=self.advbench_indices[EXACT_ADVBENCH_SLOTS + exact_slot],
            near_name_rule=near_name_rule,
            near_name=near_name,
            lookup_query=lookup_query,
        )
        self._record_challenges[state_id] = challenge
        self._write()
        return challenge

    def _write(self) -> None:
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "advbench_indices": list(self.advbench_indices),
            "near_name_rules": list(NEAR_NAME_RULES),
            "admission_behavior_schedule": DEFAULT_ADMISSION_BEHAVIOR_SCHEDULE,
            "pressure_levels": list(DEFAULT_PRESSURE_LEVELS),
            "record_challenges": {
                state_id: asdict(challenge)
                for state_id, challenge in sorted(self._record_challenges.items())
            },
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f"{self.path.name}.tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)


def load_or_create_challenge_manifest(
    root: Path,
    *,
    advbench_indices: tuple[int, ...],
) -> ChallengeManifest:
    """Create one per-run panel or prove that a resumed run uses the same one."""
    _validate_advbench_indices(advbench_indices)
    path = Path(root) / "challenge-manifest.json"
    if not path.is_file():
        manifest = ChallengeManifest(
            path=path,
            advbench_indices=advbench_indices,
            record_challenges={},
        )
        manifest._write()
        return manifest
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError("challenge manifest is unreadable") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != _SCHEMA_VERSION:
        raise ValueError("challenge manifest schema is unsupported")
    stored_indices = tuple(payload.get("advbench_indices", ()))
    _validate_advbench_indices(stored_indices)
    if stored_indices != advbench_indices:
        raise ValueError("challenge manifest AdvBench panel does not match this run")
    if tuple(payload.get("near_name_rules", ())) != NEAR_NAME_RULES:
        raise ValueError("challenge manifest near-name rules do not match this run")
    raw_challenges = payload.get("record_challenges", {})
    if not isinstance(raw_challenges, dict):
        raise TypeError("challenge manifest record registrations are malformed")
    challenges: dict[str, RecordChallenge] = {}
    for state_id, raw in raw_challenges.items():
        if not isinstance(raw, dict):
            raise TypeError("challenge manifest record registration is malformed")
        challenge = RecordChallenge(**raw)
        _validate_state_id(challenge.state_id)
        if challenge.state_id != state_id:
            raise ValueError("challenge manifest record registration has mismatched ID")
        challenges[state_id] = challenge
    return ChallengeManifest(
        path=path,
        advbench_indices=stored_indices,
        record_challenges=challenges,
    )


def _validate_advbench_indices(indices: tuple[int, ...]) -> None:
    if len(indices) != ADVBENCH_PANEL_SIZE:
        raise ValueError("challenge manifest requires exactly 16 AdvBench indices")
    if any(type(index) is not int or index < 0 for index in indices):
        raise ValueError("challenge manifest AdvBench indices must be non-negative integers")
    if len(set(indices)) != ADVBENCH_PANEL_SIZE:
        raise ValueError("challenge manifest AdvBench indices must be unique")


def _validate_state_id(state_id: str) -> None:
    if not isinstance(state_id, str) or not state_id.strip() or "/" in state_id or "\\" in state_id:
        raise ValueError("memory state ID must be a non-empty path-free value")


def _available_near_name(
    state_id: str,
    *,
    starting_rule_index: int,
    occupied: set[str],
) -> tuple[str, str]:
    for offset in range(len(NEAR_NAME_RULES)):
        rule = NEAR_NAME_RULES[(starting_rule_index + offset) % len(NEAR_NAME_RULES)]
        candidate = _derive_near_name(state_id, rule)
        if candidate != state_id and candidate not in occupied:
            return candidate, rule
    raise ValueError(f"no fixed near-name challenge is available for {state_id!r}")


def _derive_near_name(state_id: str, rule: str) -> str:
    if rule == "append_plural_s":
        return f"{state_id}s"
    if rule == "repeat_last_character":
        return f"{state_id}{state_id[-1]}"
    if rule == "transpose_last_two_characters" and len(state_id) > 1:
        return f"{state_id[:-2]}{state_id[-1]}{state_id[-2]}"
    raise ValueError(f"unsupported near-name rule: {rule}")
