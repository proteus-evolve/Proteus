"""Three small codecs; method-specific update semantics belong in plugin codecs.

Codecs transform whole immutable values. No patch is executed as Python or a shell
command. File renames are simultaneous, followed by writes; overlapping operations
are rejected instead of depending on iteration order.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from proteus.core.contracts import DIGEST, UNIQUE, ArtifactComponent, ArtifactRef, PluginRef, require
from proteus.core.serialization import Contract, ContractError, canonical_json, read_json


def bytes_hash(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class FileContent:
    """In-process byte value. FileDelta uses base64 for a JSON-replayable wire value."""

    data: bytes
    executable: bool = False

    def __post_init__(self):
        require(type(self.data) is bytes and type(self.executable) is bool, "invalid file content")


def components(files: Mapping[str, FileContent]) -> tuple[ArtifactComponent, ...]:
    require(isinstance(files, Mapping) and all(type(path) is str for path in files),
            "artifact files must be a path mapping")
    require(all(type(value) is FileContent for value in files.values()), "expected FileContent values")
    entries = tuple(ArtifactComponent(path=path, content_hash=bytes_hash(item.data),
                                     size_bytes=len(item.data), executable=item.executable)
                    for path, item in sorted(files.items()))
    # Reuse path/prefix/case collision checks even before a manifest is published.
    ArtifactRef(content_hash=bytes_hash(b""), codec=PluginRef(name="validation", version="1"),
                components=entries)
    return entries


@dataclass(frozen=True, kw_only=True)
class TextComponents(Contract):
    components: Mapping[str, str]


@dataclass(frozen=True, kw_only=True)
class StructuredRecords(Contract):
    records: Mapping[str, Mapping[str, Any]]

    def _validate(self):
        require(all(key.strip() for key in self.records), "record IDs must be nonempty")


@dataclass(frozen=True, kw_only=True)
class TextDelta(Contract):
    base_hash: str = field(metadata=DIGEST)
    put: Mapping[str, str] = field(default_factory=dict)
    delete: tuple[str, ...] = field(default=(), metadata=UNIQUE)


@dataclass(frozen=True, kw_only=True)
class RecordsDelta(Contract):
    base_hash: str = field(metadata=DIGEST)
    put: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    delete: tuple[str, ...] = field(default=(), metadata=UNIQUE)


@dataclass(frozen=True, kw_only=True)
class FileWrite(Contract):
    path: str
    base64: str
    executable: bool = False

    def _validate(self):
        self.content()
        ArtifactComponent(path=self.path, content_hash=bytes_hash(b""), size_bytes=0)

    def content(self) -> FileContent:
        try:
            data = base64.b64decode(self.base64, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ContractError("file content must be canonical base64") from exc
        require(base64.b64encode(data).decode("ascii") == self.base64,
                "file content must be canonical base64")
        return FileContent(data, self.executable)

    @classmethod
    def from_bytes(cls, path: str, data: bytes, *, executable: bool = False):
        return cls(path=path, base64=base64.b64encode(data).decode("ascii"), executable=executable)


@dataclass(frozen=True, kw_only=True)
class Rename(Contract):
    source: str
    destination: str


@dataclass(frozen=True, kw_only=True)
class FileDelta(Contract):
    base_hash: str = field(metadata=DIGEST)
    put: tuple[FileWrite, ...] = ()
    delete: tuple[str, ...] = field(default=(), metadata=UNIQUE)
    rename: tuple[Rename, ...] = ()

    def _validate(self):
        from proteus.core.contracts import unique
        unique((item.path for item in self.put), "file write")
        unique((item.source for item in self.rename), "rename source")
        unique((item.destination for item in self.rename), "rename destination")


class Codec(Protocol):
    delta_schema: type[Contract]

    def encode(self, value: Any) -> Mapping[str, FileContent]: ...
    def decode(self, files: Mapping[str, FileContent]) -> Any: ...
    def apply(self, value: Any, delta: Contract) -> Any: ...


def _updated(base, put, delete):
    require(set(delete) <= set(base), "delta deletes a missing key")
    require(not (set(put) & set(delete)), "delta both deletes and writes a key")
    return {**{key: value for key, value in base.items() if key not in delete}, **put}


class TextCodec:
    delta_schema = TextDelta

    def encode(self, value):
        require(type(value) is TextComponents, "text codec requires TextComponents")
        files = {path: FileContent(text.encode("utf-8")) for path, text in value.components.items()}
        components(files)
        return files

    def decode(self, files):
        require(not any(item.executable for item in files.values()), "text components are not executable")
        return TextComponents(components={path: item.data.decode("utf-8") for path, item in files.items()})

    def apply(self, value, delta):
        return TextComponents(components=_updated(value.components, delta.put, delta.delete))


class RecordsCodec:
    delta_schema = RecordsDelta

    def encode(self, value):
        require(type(value) is StructuredRecords, "records codec requires StructuredRecords")
        return {"records.json": FileContent(canonical_json(value).encode("utf-8"))}

    def decode(self, files):
        require(set(files) == {"records.json"} and not files["records.json"].executable,
                "records codec requires exactly records.json")
        value = StructuredRecords.from_dict(read_json(files["records.json"].data.decode("utf-8")))
        require(self.encode(value) == files, "records must use canonical encoding")
        return value

    def apply(self, value, delta):
        return StructuredRecords(records=_updated(value.records, delta.put, delta.delete))


class FileTreeCodec:
    delta_schema = FileDelta

    def encode(self, value):
        components(value)
        return dict(value)

    def decode(self, files):
        from types import MappingProxyType
        components(files)
        return MappingProxyType(dict(files))

    def apply(self, value, delta):
        sources = {item.source for item in delta.rename}
        targets = {item.destination for item in delta.rename}
        writes = {item.path: item.content() for item in delta.put}
        require(sources <= set(value), "rename source is missing")
        require(not sources.intersection(delta.delete), "rename source is also deleted")
        require(not targets.intersection((*delta.delete, *writes)), "rename target overlaps another edit")
        require(not (targets & (set(value) - sources)), "rename would overwrite an existing file")
        updated = _updated(value, writes, delta.delete)
        for source in sources:
            require(source not in writes, "rename source is also written")
            del updated[source]
        updated.update({item.destination: value[item.source] for item in delta.rename})
        components(updated)
        return updated


BUILTIN_CODECS = {"proteus.text_components": TextCodec, "proteus.structured_records": RecordsCodec,
                  "proteus.file_tree": FileTreeCodec}
