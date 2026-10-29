"""Explicit file inputs and captured outputs for fresh coding-actor workspaces."""

from dataclasses import dataclass, field
from collections.abc import Mapping

from proteus.core.contracts import ArtifactRef, DataRef, require
from proteus.core.serialization import Contract


@dataclass(frozen=True, kw_only=True)
class HarnessEpisode(Contract):
    number: int = field(metadata={"minimum": 1})
    prompts: Mapping[str, str]
    phases: tuple[str, ...]
    max_turns: int = field(default=100, metadata={"minimum": 0})
    min_turns_per_phase: int = 0
    phase_turns: Mapping[str, int] = field(default_factory=dict)
    hard_max_turns: int = 0
    checkpoint_turns: int = 0
    announce_budget: bool = False
    seed: int = 0

    def _validate(self):
        from proteus.core.budget import make_budget_plan, validate_phases
        validate_phases(self.phases)
        require(set(self.prompts) == set(self.phases) and all(self.prompts.values()), "episode prompts differ")
        make_budget_plan(max_turns=self.max_turns, min_turns_per_phase=self.min_turns_per_phase,
            phase_turns=self.phase_turns, hard_max_turns=self.hard_max_turns,
            checkpoint_turns=self.checkpoint_turns, phases=self.phases)


@dataclass(frozen=True, kw_only=True)
class WorkspaceInput(Contract):
    seed: ArtifactRef
    history: DataRef
    active_seed: ArtifactRef | None = None
    episode: HarnessEpisode | None = None
    max_output_bytes: int = field(default=8 * 1024 * 1024, metadata={'minimum': 1})
    max_output_files: int = field(default=256, metadata={'minimum': 1})

    def to_dict(self):
        value = super().to_dict()
        if self.active_seed is None:
            value.pop('active_seed')
            value.pop('episode')  # Ordinary coding requests retain their old shape.
        return value

    def _validate(self):
        require(self.seed.codec.name == 'proteus.file_tree' and self.seed.codec.version == '1',
                'coding workspaces require the versioned file-tree codec')
        require((self.active_seed is not None) == (self.episode is not None), "evolution needs active state and episode control")
        require(self.active_seed is None or self.active_seed.codec == self.seed.codec, "active candidate codec differs")
        require(self.history.kind == 'proteus.evidence.view@1', 'coding history must be an evidence view')


@dataclass(frozen=True, kw_only=True)
class WorkspaceOptions(Contract):
    max_output_bytes: int = field(default=8 * 1024 * 1024, metadata={'minimum': 1})
    max_output_files: int = field(default=256, metadata={'minimum': 1})
