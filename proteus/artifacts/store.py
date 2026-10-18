"""Immutable artifact storage with verified bytes and copy-only materialization.

The filesystem is trusted storage, not a security boundary. Every read rechecks
identity; immutable publications use exclusive links, so retries can neither replace
an existing object nor mutate a parent. A process crash may leave unreferenced blobs,
but never a manifest pointing at partially written bytes.
"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from proteus.artifacts.codecs import FileContent, bytes_hash, components
from proteus.core.contracts import (
    DIGEST, TEXT, ArtifactComponent, ArtifactRef, Candidate, DataRef, PluginRef, ValidationStatus, require,
)
from proteus.core.serialization import Contract, canonical_json, content_hash
from proteus.recipes.config import PluginSpec


def _read(path: Path, *, max_bytes: int | None = None) -> bytes:
    # A symlink/special file must never masquerade as immutable content.
    require(stat.S_ISREG(path.lstat().st_mode), f"not a regular stored file: {path}")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        require(stat.S_ISREG(before.st_mode), f"not a regular file: {path}")
        require(max_bytes is None or before.st_size <= max_bytes, 'file exceeds the declared byte limit')
        data = stream.read() if max_bytes is None else stream.read(max_bytes + 1)
        require(max_bytes is None or len(data) <= max_bytes, 'file exceeds the declared byte limit')
        after = os.fstat(stream.fileno())
    # ctime also changes when a publisher removes its temporary hardlink; that
    # metadata-only change is compatible with concurrent immutable readers.
    require((before.st_size, before.st_mtime_ns, before.st_mode)
            == (after.st_size, after.st_mtime_ns, after.st_mode),
            f"file changed while being read: {path}")
    return data


def _sync_directory(path: Path) -> None:
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _mkdir_durable(path: Path) -> None:
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    require(current.is_dir() and not current.is_symlink(), "store parent must be a real directory")
    for directory in reversed(missing):
        try:
            directory.mkdir()
        except FileExistsError:
            require(directory.is_dir() and not directory.is_symlink(), "store path must be a real directory")
        _sync_directory(directory.parent)


def publish_bytes(path: Path, data: bytes) -> None:
    """Durable no-replace publication. Identical retries are idempotent."""
    _mkdir_durable(path.parent)
    descriptor, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o444)
        try:
            os.link(temporary, path)
        except FileExistsError:
            require(_read(path) == data, f"immutable object already exists with different content: {path}")
        _sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass(frozen=True, kw_only=True)
class ArtifactManifest(Contract):
    codec: PluginRef
    components: tuple[ArtifactComponent, ...]
    runtime: PluginRef | None = None
    environment_hash: str | None = field(default=None, metadata=DIGEST)

    def _validate(self):
        require(tuple(item.path for item in self.components)
                == tuple(sorted(item.path for item in self.components)), "manifest must be ordered")
        # ArtifactRef performs portable path and digest validation.
        self.ref()

    def ref(self) -> ArtifactRef:
        return ArtifactRef(content_hash=content_hash(self), codec=self.codec, components=self.components,
                           runtime=self.runtime, environment_hash=self.environment_hash)


@dataclass(frozen=True, kw_only=True)
class CandidateEntry(Contract):
    candidate: Candidate
    origin: Literal["recipe", "native_checkpoint_import"] = "recipe"
    source_checkpoint: str | None = field(default=None, metadata=TEXT)

    def _validate(self):
        require((self.origin == "native_checkpoint_import") == (self.source_checkpoint is not None),
                "native checkpoint imports require explicit provenance")


@dataclass(frozen=True, kw_only=True)
class CandidateDecision(Contract):
    run_id: str = field(metadata=TEXT)
    candidate_id: str = field(metadata=TEXT)
    operation_id: str = field(metadata=TEXT)
    attempt_id: str = field(metadata=TEXT)
    disposition: Literal["accepted", "rejected", "failed"]
    reason: str = field(metadata=TEXT)
    evidence: tuple[DataRef, ...] = ()


class ArtifactStore:
    def __init__(self, root: str | Path, registry, *, allowed_codecs: tuple[PluginRef, ...] | None = None):
        self.root = Path(root).resolve()
        self.registry = registry
        self.allowed_codecs = allowed_codecs

    def _path(self, collection: str, digest: str, suffix: str = "") -> Path:
        # Caller-supplied strings never become path segments without digest validation.
        DataRef(kind=collection, id=digest, content_hash=digest)
        return self.root / collection / (digest.removeprefix("sha256:") + suffix)

    def _codec(self, ref: PluginRef):
        require(self.allowed_codecs is None or ref in self.allowed_codecs,
                "artifact codec is not declared in this run's locked recipe")
        return self.registry.create(PluginSpec(plugin=ref), "codec")

    def put(self, codec: PluginRef, value, *, runtime: PluginRef | None = None,
            environment_hash: str | None = None) -> ArtifactRef:
        implementation = self._codec(codec)
        files = implementation.encode(value)
        manifest = ArtifactManifest(codec=codec, components=components(files), runtime=runtime,
                                    environment_hash=environment_hash)
        # Demand a stable encoding from third-party codecs too.
        require(implementation.encode(implementation.decode(files)) == files,
                "codec does not round-trip its encoding")
        for item in manifest.components:
            publish_bytes(self._path("blobs", item.content_hash), files[item.path].data)
        ref = manifest.ref()
        publish_bytes(self._path("manifests", ref.content_hash, ".json"),
                      canonical_json(manifest).encode("utf-8"))
        return ref

    def manifest(self, ref: ArtifactRef) -> ArtifactManifest:
        path = self._path("manifests", ref.content_hash, ".json")
        manifest = ArtifactManifest.from_json(_read(path).decode("utf-8"))
        require(manifest.ref() == ref, "artifact reference differs from stored manifest")
        return manifest

    def files(self, ref: ArtifactRef) -> Mapping[str, FileContent]:
        from types import MappingProxyType
        manifest = self.manifest(ref)
        files = {}
        for item in manifest.components:
            data = _read(self._path("blobs", item.content_hash))
            require(len(data) == item.size_bytes and bytes_hash(data) == item.content_hash,
                    f"artifact blob is corrupt: {item.path}")
            files[item.path] = FileContent(data, item.executable)
        return MappingProxyType(files)

    def get(self, ref: ArtifactRef):
        return self._codec(ref.codec).decode(self.files(ref))

    def validate(self, ref: ArtifactRef) -> None:
        """Check identity, complete bytes and codec invariants; no build/boot is implied."""
        files = self.files(ref)
        codec = self._codec(ref.codec)
        require(codec.encode(codec.decode(files)) == files, "artifact codec validation failed")

    def apply_delta(self, base: ArtifactRef, delta: Contract) -> ArtifactRef:
        codec = self._codec(base.codec)
        require(type(delta) is codec.delta_schema, "delta has the wrong codec schema")
        require(delta.base_hash == base.content_hash, "delta was proposed against a different base")
        value = codec.apply(self.get(base), delta)
        return self.put(base.codec, value, runtime=base.runtime, environment_hash=base.environment_hash)

    @staticmethod
    def read_tree(root: str | Path, *, max_bytes: int | None = None,
                  max_files: int | None = None) -> Mapping[str, FileContent]:
        """Capture regular files and executable bits, rejecting links and special files.

        Empty directories are not artifacts. There are no implicit ignore rules: callers
        must select a clean source tree explicitly, including their declared source files.
        """
        root = Path(root)
        require(max_bytes is None or (type(max_bytes) is int and max_bytes >= 0), 'invalid tree byte limit')
        require(max_files is None or (type(max_files) is int and max_files >= 0), 'invalid tree file limit')
        require(root.is_dir() and not root.is_symlink(), "tree root must be a real directory")
        files = {}
        remaining = max_bytes
        def fail_walk(error):
            raise error
        for current, directories, names in os.walk(root, followlinks=False, onerror=fail_walk):
            for name in directories:
                path = Path(current) / name
                require(stat.S_ISDIR(path.lstat().st_mode), "artifact tree cannot contain symlinks")
            for name in names:
                require(max_files is None or len(files) < max_files, 'tree exceeds the declared file limit')
                path = Path(current) / name
                mode = path.lstat().st_mode
                require(stat.S_ISREG(mode), "artifact tree supports only regular files")
                data = _read(path, max_bytes=remaining)
                if remaining is not None:
                    remaining -= len(data)
                files[path.relative_to(root).as_posix()] = FileContent(data, bool(mode & 0o111))
        components(files)
        return files

    def capture_tree(self, root: str | Path, *, runtime: PluginRef | None = None,
                     environment_hash: str | None = None) -> ArtifactRef:
        return self.put(PluginRef(name="proteus.file_tree", version="1"), self.read_tree(root),
                        runtime=runtime, environment_hash=environment_hash)

    def verify_tree(self, ref: ArtifactRef, root: str | Path) -> None:
        require(self.read_tree(root) == self.files(ref), "materialized tree differs from candidate")

    def materialize(self, ref: ArtifactRef, destination: str | Path) -> Path:
        """Copy into a fresh directory only. Old files/build caches cannot survive.

        Do not overlay a candidate on a prior workspace: use a new workspace and keep
        method state separately. Copies never hardlink writable execution to store blobs.
        """
        self.validate(ref)
        files = self.files(ref)
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        require(not destination.exists() and not destination.is_symlink(),
                "materialization requires a fresh destination")
        destination.mkdir(exist_ok=False)
        try:
            for name, item in files.items():
                path = destination / name
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("xb") as stream:
                    stream.write(item.data)
                path.chmod(0o755 if item.executable else 0o644)
            self.verify_tree(ref, destination)
        except BaseException:
            shutil.rmtree(destination)
            raise
        return destination

    def copy_from(self, source: ArtifactStore, ref: ArtifactRef) -> None:
        source.validate(ref)
        copied = self.put(ref.codec, source.get(ref), runtime=ref.runtime,
                          environment_hash=ref.environment_hash)
        require(copied == ref, "artifact import changed identity")

    def _candidate_path(self, run_id: str, candidate_id: str) -> Path:
        return self._path("candidates", content_hash([run_id, candidate_id]), ".json")

    def register_candidate(self, candidate: Candidate, *, source_checkpoint: str | None = None) -> None:
        self.validate(candidate.artifact)
        for parent in candidate.parent_ids:
            self.candidate(candidate.run_id, parent)
        entry = CandidateEntry(candidate=candidate, source_checkpoint=source_checkpoint,
            origin="native_checkpoint_import" if source_checkpoint is not None else "recipe")
        digest = content_hash(entry)
        publish_bytes(self._path("candidate-entries", digest, ".json"), canonical_json(entry).encode("utf-8"))
        ref = DataRef(kind="proteus.candidate_entry@1", id=content_hash([candidate.run_id, candidate.id]),
                      content_hash=digest)
        publish_bytes(self._candidate_path(candidate.run_id, candidate.id), canonical_json(ref).encode("utf-8"))

    def candidate_entry(self, run_id: str, candidate_id: str) -> CandidateEntry:
        ref = DataRef.from_json(_read(self._candidate_path(run_id, candidate_id)).decode("utf-8"))
        require(ref.kind == "proteus.candidate_entry@1" and ref.id == content_hash([run_id, candidate_id]),
                "stored candidate index mismatch")
        entry = CandidateEntry.from_json(_read(self._path("candidate-entries", ref.content_hash, ".json"))
                                         .decode("utf-8"))
        require(content_hash(entry) == ref.content_hash, "stored candidate entry is corrupt")
        require(entry.candidate.run_id == run_id and entry.candidate.id == candidate_id,
                "stored candidate identity mismatch")
        self.validate(entry.candidate.artifact)
        return entry

    def candidate(self, run_id: str, candidate_id: str) -> Candidate:
        return self.candidate_entry(run_id, candidate_id).candidate

    def fork(self, parent: Candidate, *, candidate_id: str, created_by: str,
             created_attempt_id: str, delta: Contract | None = None,
             validation: ValidationStatus = ValidationStatus.UNCHECKED) -> Candidate:
        require(self.candidate(parent.run_id, parent.id) == parent, "fork parent mismatch")
        artifact = parent.artifact if delta is None else self.apply_delta(parent.artifact, delta)
        child = Candidate(id=candidate_id, run_id=parent.run_id, artifact=artifact,
                          created_by=created_by, created_attempt_id=created_attempt_id,
                          parent_ids=(parent.id,), validation=validation)
        self.register_candidate(child)
        return child

    def decide(self, decision: CandidateDecision) -> str:
        """Append evidence without deleting rejected/failed artifacts or rewriting lineage."""
        self.candidate(decision.run_id, decision.candidate_id)
        digest = content_hash(decision)
        publish_bytes(self._path("decisions", digest, ".json"), canonical_json(decision).encode("utf-8"))
        return digest

    def decisions(self, run_id: str, candidate_id: str) -> tuple[CandidateDecision, ...]:
        self.candidate(run_id, candidate_id)
        result = []
        for path in sorted((self.root / "decisions").glob("*.json")):
            decision = CandidateDecision.from_json(_read(path).decode("utf-8"))
            require(path == self._path("decisions", content_hash(decision), ".json"),
                    "stored decision identity mismatch")
            if decision.run_id == run_id and decision.candidate_id == candidate_id:
                result.append(decision)
        return tuple(result)
