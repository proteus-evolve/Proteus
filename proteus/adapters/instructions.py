"""Shared disposition carrier for harnesses that read an instructions file natively.

Many external harnesses (dsh, pi, most coding agents) load `AGENTS.md` from the workspace.
That makes the instructions file the natural, removable disposition carrier: the
perturbation is a marked block appended to the file, removal is deleting the block, and
the fingerprint is the hash of the block alone — so agent edits to the rest of the file
do not read as disposition drift.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from proteus.core.disposition import Disposition

OPEN = "<!-- proteus:disposition -->"
CLOSE = "<!-- /proteus:disposition -->"


def install_block(path: Path, disposition: Disposition) -> None:
    """Replace any existing disposition block; NEUTRAL leaves no block at all."""
    text = path.read_text(encoding="utf-8")
    if OPEN in text:
        head, _, rest = text.partition(OPEN)
        _, _, tail = rest.partition(CLOSE)
        text = (head + tail).rstrip() + "\n"
    parts = []
    if disposition.prompt_suffix:
        parts.append(disposition.prompt_suffix.strip())
    if disposition.per_phase:
        parts.append("Phase-specific action preferences:\n" + "\n".join(
            f"- During {phase}: {instruction.strip()}"
            for phase, instruction in disposition.per_phase.items()
            if instruction.strip()
        ))
    carrier = "\n\n".join(part for part in parts if part)
    if not disposition.is_empty and carrier:
        text = (text.rstrip() + "\n\n" + OPEN + "\n"
                + carrier + "\n" + CLOSE + "\n")
    path.write_text(text, encoding="utf-8")


def block_fingerprint(path: Path) -> str:
    """Hash of the installed block only ('' state hashes to the same value everywhere)."""
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8")
    block = text.split(OPEN, 1)[1].split(CLOSE, 1)[0] if OPEN in text else ""
    return hashlib.sha256(block.encode()).hexdigest()[:16]
