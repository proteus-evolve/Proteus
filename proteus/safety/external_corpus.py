"""Controller-private input boundary for fixed external pressure corpora."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

PAUL_GRAHAM_CORPUS_ID = "paul_graham_panel_v1"
PAUL_GRAHAM_PANEL_SIZE = 64


class ExternalCorpusUnavailable(ValueError):
    """The operator-staged corpus cannot support a reproducible pressure trial."""


@dataclass(frozen=True)
class PaulGrahamSource:
    source_ordinal: int
    source_id: str
    title: str
    source_url: str
    local_path: str
    acquired_at: str
    normalized_whitespace_token_count: int


@dataclass(frozen=True)
class PaulGrahamPanel:
    corpus_id: str
    sources: tuple[PaulGrahamSource, ...]

    @property
    def normalized_whitespace_token_count(self) -> int:
        return sum(source.normalized_whitespace_token_count for source in self.sources)


def normalized_whitespace_token_count(text: str) -> int:
    """Use the fixed corpus metric, deliberately independent of provider tokenizers."""
    return len(text.split())


def read_panel_source(source: PaulGrahamSource) -> str:
    """Read a validated private source only while building a disposable trial."""
    try:
        return Path(source.local_path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ExternalCorpusUnavailable(
            f"external corpus source is unavailable: {source.source_id}"
        ) from exc


def load_paul_graham_panel(root: Path) -> PaulGrahamPanel:
    """Load exactly one private, fixed 64-essay panel without contacting a network."""
    root = Path(root)
    manifest_path = root / "manifest.json"
    try:
        import json

        raw: Any = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ExternalCorpusUnavailable("external corpus manifest is unavailable") from exc
    if not isinstance(raw, dict):
        raise ExternalCorpusUnavailable("external corpus manifest must be an object")
    corpus_id = raw.get("corpus_id")
    if corpus_id != PAUL_GRAHAM_CORPUS_ID:
        raise ExternalCorpusUnavailable(
            f"external corpus must declare {PAUL_GRAHAM_CORPUS_ID!r}"
        )
    raw_sources = raw.get("sources")
    if not isinstance(raw_sources, list) or len(raw_sources) != PAUL_GRAHAM_PANEL_SIZE:
        raise ExternalCorpusUnavailable(
            f"external corpus must contain exactly {PAUL_GRAHAM_PANEL_SIZE} sources"
        )

    resolved_root = root.resolve()
    sources = tuple(
        _load_source(raw_source, root=root, resolved_root=resolved_root)
        for raw_source in raw_sources
    )
    ordinals = [source.source_ordinal for source in sources]
    if sorted(ordinals) != list(range(PAUL_GRAHAM_PANEL_SIZE)):
        raise ExternalCorpusUnavailable("external corpus source ordinals must be contiguous")
    if len({source.source_id for source in sources}) != PAUL_GRAHAM_PANEL_SIZE:
        raise ExternalCorpusUnavailable("external corpus source IDs must be unique")
    if len({source.local_path for source in sources}) != PAUL_GRAHAM_PANEL_SIZE:
        raise ExternalCorpusUnavailable("external corpus source paths must be unique")
    return PaulGrahamPanel(
        corpus_id=corpus_id,
        sources=tuple(sorted(sources, key=lambda source: source.source_ordinal)),
    )


def _load_source(
    raw_source: object,
    *,
    root: Path,
    resolved_root: Path,
) -> PaulGrahamSource:
    if not isinstance(raw_source, dict):
        raise ExternalCorpusUnavailable("external corpus source entry must be an object")
    try:
        ordinal = raw_source["source_ordinal"]
        source_id = raw_source["source_id"]
        title = raw_source["title"]
        source_url = raw_source["source_url"]
        relative_path = raw_source["local_path"]
        acquired_at = raw_source["acquired_at"]
        declared_tokens = raw_source["normalized_whitespace_token_count"]
    except KeyError as exc:
        raise ExternalCorpusUnavailable(
            f"external corpus source is missing {exc.args[0]!r}"
        ) from exc
    if type(ordinal) is not int or ordinal < 0:
        raise ExternalCorpusUnavailable("external corpus source ordinal must be non-negative")
    if not all(
        isinstance(value, str) and value.strip()
        for value in (source_id, title, source_url, relative_path, acquired_at)
    ):
        raise ExternalCorpusUnavailable("external corpus source metadata must be non-empty text")
    if type(declared_tokens) is not int or declared_tokens <= 0:
        raise ExternalCorpusUnavailable("external corpus source token count must be positive")
    source_path = (root / relative_path).resolve()
    if source_path != resolved_root and resolved_root not in source_path.parents:
        raise ExternalCorpusUnavailable("external corpus source path must stay below its root")
    try:
        text = source_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ExternalCorpusUnavailable(
            f"external corpus source is unavailable: {source_id}"
        ) from exc
    if "\r" in text:
        raise ExternalCorpusUnavailable("external corpus source must use LF line endings")
    if any(line.rstrip(" \t") != line for line in text.split("\n")):
        raise ExternalCorpusUnavailable("external corpus source must not have trailing whitespace")
    actual_tokens = normalized_whitespace_token_count(text)
    if actual_tokens != declared_tokens:
        raise ExternalCorpusUnavailable(
            f"external corpus source token count disagrees: {source_id}"
        )
    return PaulGrahamSource(
        source_ordinal=ordinal,
        source_id=source_id,
        title=title,
        source_url=source_url,
        local_path=str(source_path),
        acquired_at=acquired_at,
        normalized_whitespace_token_count=declared_tokens,
    )
