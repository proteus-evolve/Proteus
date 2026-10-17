"""Immutable experiment manifests and exact dependency/source locks."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from proteus.core.contracts import DIGEST, TEXT, UNIQUE, PluginRef, require, unique
from proteus.core.serialization import Contract, content_hash
from proteus.recipes.config import RecipeConfig


@dataclass(frozen=True, kw_only=True)
class SourcePin(Contract):
    name: str = field(metadata=TEXT)
    repository: str = field(metadata=TEXT)
    revision: str = field(metadata={"pattern": r"^[0-9a-f]{40}([0-9a-f]{24})?$"})
    dirty_patch_hash: str | None = field(default=None, metadata=DIGEST)


@dataclass(frozen=True, kw_only=True)
class DependencyPin(Contract):
    name: str = field(metadata=TEXT)
    version: str = field(metadata={"pattern": r"^[0-9][A-Za-z0-9.!+_-]*$"})
    distribution_hash: str | None = field(default=None, metadata=DIGEST)


@dataclass(frozen=True, kw_only=True)
class PluginLock(Contract):
    plugin: PluginRef
    implementation_hash: str = field(metadata=DIGEST)
    source: str = field(metadata=TEXT)
    capabilities: tuple[str, ...] = field(default=(), metadata=UNIQUE)


@dataclass(frozen=True, kw_only=True)
class DependencyLock(Contract):
    python_version: str = field(metadata={"pattern": r"^[0-9]+\.[0-9]+\.[0-9]+[A-Za-z0-9.]*$"})
    platform: str = field(metadata=TEXT)
    sources: tuple[SourcePin, ...] = field(metadata={"minItems": 1})
    plugins: tuple[PluginLock, ...] = field(metadata={"minItems": 1})
    packages: tuple[DependencyPin, ...] = ()

    def _validate(self):
        import re
        unique((source.name for source in self.sources), "source pin")
        unique(((item.plugin.name, item.plugin.version) for item in self.plugins), "plugin lock")
        unique((re.sub(r"[-_.]+", "-", item.name).lower() for item in self.packages), "package pin")
        names = {source.name for source in self.sources}
        require("proteus" in names, "lock must include the proteus framework source")
        require(all(item.source in names for item in self.plugins),
                "plugin lock references an unknown source")


@dataclass(frozen=True, kw_only=True)
class RunManifest(Contract):
    run_id: str = field(metadata=TEXT)
    created_at: str = field(metadata=TEXT)
    recipe: RecipeConfig
    lock: DependencyLock
    config_hash: str = field(metadata=DIGEST)
    lock_hash: str = field(metadata=DIGEST)

    def _validate(self):
        try:
            instant = datetime.fromisoformat(self.created_at.replace("Z", "+00:00"))
        except ValueError as exc:
            from proteus.core.serialization import ContractError
            raise ContractError("created_at must be an ISO-8601 timestamp") from exc
        require(instant.tzinfo is not None, "created_at must include a timezone")
        require(self.config_hash == self.recipe.config_hash, "recipe content does not match config_hash")
        require(self.lock_hash == content_hash(self.lock), "dependencies do not match lock_hash")
        locked = {(item.plugin.name, item.plugin.version): item for item in self.lock.plugins}
        for plugin in self.recipe.plugin_refs():
            require((plugin.name, plugin.version) in locked,
                    f"plugin is not locked: {plugin.name}@{plugin.version}")
        self.recipe.check_capabilities({key: item.capabilities for key, item in locked.items()})

    @classmethod
    def create(cls, *, run_id: str, recipe: RecipeConfig, lock: DependencyLock,
               created_at: str | None = None) -> RunManifest:
        return cls(run_id=run_id, created_at=created_at or datetime.now(timezone.utc).isoformat(),
                   recipe=recipe, lock=lock, config_hash=recipe.config_hash, lock_hash=content_hash(lock))

    @property
    def manifest_hash(self) -> str:
        return content_hash(self)

    def assert_resume_compatible(self, recipe: RecipeConfig, lock: DependencyLock) -> None:
        require(recipe.config_hash == self.config_hash, "resume changes the resolved recipe")
        require(content_hash(lock) == self.lock_hash, "resume changes the dependency lock")

    def write(self, path: str | Path) -> Path:
        """Atomically create a manifest, or verify an identical existing file.

        A hard link publishes a fully written file without replacing another writer's
        manifest. Unsupported filesystems raise rather than weaken the no-replace rule.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = (self.to_json() + "\n").encode("utf-8")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".manifest-", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                require(path.read_bytes() == encoded, "refusing to overwrite a different manifest")
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return path
