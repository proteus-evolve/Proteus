"""Versioned ACE playbooks with exact text and reference counter/ADD semantics.

The text is authoritative; the index is derived and checked on every read. This
keeps prompt whitespace and section ordering out of JSON's key-order semantics.
The next ID is explicit state, including IDs allocated by earlier publications.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from proteus.artifacts.codecs import FileContent
from proteus.core.contracts import DIGEST, PluginRef, require, unique
from proteus.core.plugins import EmptyOptions, Plugin, implementation_hash
from proteus.core.serialization import Contract, ContractError, canonical_json, json_value
from proteus.methods.ace import upstream


REPOSITORY = "https://github.com/ace-agent/ace"
REVISION = "82709de050e1db6e6ef2f07bcb0393560b94992a"
CODEC = PluginRef(name="proteus.ace_playbook", version="1")
EMPTY_PLAYBOOK = "\n\n".join((
    "## STRATEGIES & INSIGHTS", "## FORMULAS & CALCULATIONS", "## CODE SNIPPETS & TEMPLATES",
    "## COMMON MISTAKES TO AVOID", "## PROBLEM-SOLVING HEURISTICS",
    "## CONTEXT CLUES & INDICATORS", "## OTHERS",
))


@dataclass(frozen=True, kw_only=True)
class Bullet(Contract):
    id: str
    section: str
    line: int = field(metadata={"minimum": 1})
    helpful: int = field(metadata={"minimum": 0})
    harmful: int = field(metadata={"minimum": 0})
    content: str


def _bullets(text):
    result, section = [], "general"
    for number, line in enumerate(text.split("\n"), 1):
        if line.strip().startswith("##"):
            section = line.strip()[2:].strip()
            continue
        parsed = upstream.parse_playbook_line(line)
        if parsed is not None:
            result.append(Bullet(id=parsed["id"], section=section, line=number,
                helpful=parsed["helpful"], harmful=parsed["harmful"], content=parsed["content"]))
    return tuple(result)


@dataclass(frozen=True, kw_only=True)
class Playbook(Contract):
    text: str
    next_id: int = field(metadata={"minimum": 1})

    def _validate(self):
        require(self.text.strip().startswith("##"), "ACE playbook must begin with a section header")
        unique((bullet.id for bullet in self.bullets), "ACE bullet ID")
        require(self.next_id >= upstream.get_next_global_id(self.text),
                "ACE next_id would reuse an existing global ID")

    @classmethod
    def from_text(cls, text=EMPTY_PLAYBOOK, *, next_id=None):
        return cls(text=text, next_id=upstream.get_next_global_id(text) if next_id is None else next_id)

    @property
    def bullets(self):
        """Parsed first lines, in prompt order; continuation lines remain in text."""
        return _bullets(self.text)

    def stats(self):
        return upstream.get_playbook_stats(self.text)

    def extract(self, bullet_ids):
        return upstream.extract_playbook_bullets(self.text, bullet_ids)


@dataclass(frozen=True, kw_only=True)
class PlaybookIndex(Contract):
    next_id: int = field(metadata={"minimum": 1})
    bullets: tuple[Bullet, ...]
    schema_version: Literal[1] = 1
    source_revision: str = REVISION

    def _validate(self):
        require(self.source_revision == REVISION, "unsupported ACE reference revision")


@dataclass(frozen=True, kw_only=True)
class PlaybookDelta(Contract):
    base_hash: str = field(metadata=DIGEST)
    kind: Literal["counters", "curate"]
    # Keep order, duplicates, ignored records and extra fields in the operation
    # evidence. No arbitrary mapping order is used as an algorithmic input.
    records: tuple[Any, ...] = ()

    def _validate(self):
        for record in self.records:
            if self.kind == "counters":
                if isinstance(record, Mapping):
                    identifier = record.get("id") or record.get("bullet", "")
                    require(not identifier or type(identifier) is str,
                            "ACE tag ID must be a string when present")
                # Upstream ignores non-object tags and missing IDs. Unknown tags
                # are neutral; preserve them instead of inventing a correction.
            else:
                require(isinstance(record, Mapping) and type(record.get("type")) is str,
                        "ACE curator operation requires a string type")
                if record["type"] == "ADD":
                    require(type(record.get("section", "general")) is str
                            and type(record.get("content", "")) is str,
                            "ACE ADD section and content must be strings")

    @property
    def ignored_operations(self):
        """Zero-based positions of unsupported curator operations; never execute them."""
        return tuple(i for i, record in enumerate(self.records)
                     if self.kind == "curate" and record["type"] != "ADD")


class PlaybookCodec:
    delta_schema = PlaybookDelta

    def encode(self, value):
        require(type(value) is Playbook, "ACE codec requires a Playbook")
        index = PlaybookIndex(next_id=value.next_id, bullets=value.bullets)
        return {"playbook.txt": FileContent(value.text.encode("utf-8")),
                "index.json": FileContent(canonical_json(index).encode("utf-8"))}

    def decode(self, files):
        require(set(files) == {"playbook.txt", "index.json"}
                and not any(item.executable for item in files.values()),
                "ACE codec requires non-executable playbook.txt and index.json")
        index = PlaybookIndex.from_json(files["index.json"].data.decode("utf-8"))
        value = Playbook(text=files["playbook.txt"].data.decode("utf-8"), next_id=index.next_id)
        require(self.encode(value) == files, "ACE index differs from text or canonical encoding")
        return value

    def apply(self, value, delta):
        require(type(value) is Playbook and type(delta) is PlaybookDelta, "invalid ACE value or delta")
        # The pinned functions expect ordinary list/dict values, not frozen JSON.
        records = json_value(delta.records)
        try:
            if delta.kind == "counters":
                text, next_id = upstream.update_bullet_counts(value.text, records), value.next_id
            else:
                text, next_id = upstream.apply_curator_operations(value.text, records, value.next_id)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ContractError(f"ACE reference rejected this update: {exc}") from exc
        # Reject ambiguous or non-curatable results before publishing any bytes.
        return Playbook(text=text, next_id=next_id)


def register(registry):
    registry.register_data("proteus.ace.playbook@1", Playbook)
    registry.register_data("proteus.ace.delta@1", PlaybookDelta)
    root = Path(__file__).parent
    registry.register(Plugin(ref=CODEC, kind="codec", options=EmptyOptions,
        factory=lambda options: PlaybookCodec(), source="proteus",
        implementation_hash=implementation_hash({path.name: path for path in root.glob("*.py")}),
        capabilities=("artifact.immutable", "artifact.delta", "artifact.materialize")))
