"""Explicit file inputs and captured outputs for fresh coding-actor workspaces."""

from dataclasses import dataclass, field

from proteus.core.contracts import ArtifactRef, DataRef, require
from proteus.core.serialization import Contract


@dataclass(frozen=True, kw_only=True)
class WorkspaceInput(Contract):
    seed: ArtifactRef
    history: DataRef
    max_output_bytes: int = field(default=8 * 1024 * 1024, metadata={'minimum': 1})
    max_output_files: int = field(default=256, metadata={'minimum': 1})

    def _validate(self):
        require(self.seed.codec.name == 'proteus.file_tree' and self.seed.codec.version == '1',
                'coding workspaces require the versioned file-tree codec')
        require(self.history.kind == 'proteus.evidence.view@1', 'coding history must be an evidence view')


@dataclass(frozen=True, kw_only=True)
class WorkspaceOptions(Contract):
    max_output_bytes: int = field(default=8 * 1024 * 1024, metadata={'minimum': 1})
    max_output_files: int = field(default=256, metadata={'minimum': 1})
