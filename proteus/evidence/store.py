"""Immutable file snapshots, with no traversal of a run's private directories.

Views are disclosures for trusted plugins, not OS isolation. Give untrusted workers
only a materialized view in an isolated backend, never the containing run directory.
"""

import shutil
from pathlib import Path

from proteus.artifacts.codecs import bytes_hash
from proteus.artifacts.store import ArtifactStore, _read, publish_bytes
from proteus.core.contracts import DataRef, require
from proteus.evidence.records import EvidenceView


class EvidenceStore:
    def __init__(self, root):
        self.root = Path(root).resolve()

    def _path(self, collection, digest):
        DataRef(kind=collection, id=digest, content_hash=digest)
        return self.root / collection / digest.removeprefix("sha256:")

    def put(self, view: EvidenceView, files: dict[str, bytes]) -> DataRef:
        require(set(files) == {entry.path for entry in view.entries}, "evidence file set differs from manifest")
        for entry in view.entries:
            data = files[entry.path]
            require(type(data) is bytes and bytes_hash(data) == entry.content_hash and len(data) == entry.size_bytes,
                    "evidence bytes differ from manifest")
        for entry in view.entries:
            publish_bytes(self._path("blobs", entry.content_hash), files[entry.path])
        ref = view.ref()
        # Only the final immutable manifest makes the snapshot addressable.
        publish_bytes(self._path("views", ref.content_hash), view.to_json().encode())
        return ref

    def get(self, ref: DataRef) -> EvidenceView:
        require(ref.kind == "proteus.evidence.view@1", "not an evidence view")
        view = EvidenceView.from_json(_read(self._path("views", ref.content_hash)).decode())
        require(view.ref() == ref, "evidence view identity mismatch")
        return view

    def read(self, ref: DataRef, path: str) -> bytes:
        view = self.get(ref)
        entry = next((entry for entry in view.entries if entry.path == path), None)
        require(entry is not None, "file is outside this evidence view")
        return self._read_entry(entry)

    def _read_entry(self, entry) -> bytes:
        data = _read(self._path("blobs", entry.content_hash))
        require(bytes_hash(data) == entry.content_hash and len(data) == entry.size_bytes,
                "evidence blob integrity mismatch")
        return data

    def verify(self, ref: DataRef) -> EvidenceView:
        view = self.get(ref)
        for entry in view.entries:
            self._read_entry(entry)
        return view

    def search(self, ref: DataRef, text: str):
        """Literal, case-sensitive UTF-8 line search; binary files remain readable."""
        require(type(text) is str and bool(text), "search text must be nonempty")
        matches = []
        for entry in self.get(ref).entries:
            data = self._read_entry(entry)
            try:
                lines = data.decode("utf-8").splitlines()
            except UnicodeDecodeError:
                continue
            matches.extend((entry.path, number, line) for number, line in enumerate(lines, 1) if text in line)
        return tuple(matches)

    def materialize(self, ref: DataRef, destination) -> Path:
        view = self.verify(ref)
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        require(not destination.exists() and not destination.is_symlink(), "evidence export needs a fresh destination")
        destination.mkdir(exist_ok=False)
        try:
            for entry in view.entries:
                path = destination / entry.path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(self._read_entry(entry))
                path.chmod(0o444)  # Copies, never writable hardlinks to the evidence store.
            # A crash before this publication leaves an incomplete export, not a valid view.
            publish_bytes(destination / "view.json", view.to_json().encode())
            self.verify_materialized(ref, destination)
        except BaseException:
            shutil.rmtree(destination)
            raise
        return destination

    def verify_materialized(self, ref: DataRef, destination) -> None:
        view = self.get(ref)
        files = ArtifactStore.read_tree(destination)
        require(set(files) == {"view.json", *(entry.path for entry in view.entries)},
                "materialized evidence file set differs")
        require(files["view.json"].data == view.to_json().encode(), "materialized evidence manifest differs")
        for entry in view.entries:
            item = files[entry.path]
            require(not item.executable and item.data == self._read_entry(entry), "materialized evidence bytes differ")
