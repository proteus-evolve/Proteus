"""Verify the upstream Python source before importing the optional GEPA runtime."""

from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path

from proteus.core.contracts import require
from proteus.core.plugins import implementation_hash


REPOSITORY = "https://github.com/gepa-ai/gepa"
REVISION = "8b0ce6cd99a234f6b74daf37558a2ac0ce18f975"
# v0.1.4's source declares distribution version 0.1.3. Version alone is not identity.
SOURCE_HASH = "sha256:ccf1e6140d7ebd2190e9ee0b1b9558b2ab9a8673366d61b748f7c43d72fcad50"
INSTALL = f"python -m pip install 'gepa @ git+{REPOSITORY}.git@{REVISION}'"


def verify_source(root: Path) -> str:
    files = {path.relative_to(root).as_posix(): path for path in root.rglob("*.py")}
    digest = implementation_hash(files)
    require(digest == SOURCE_HASH, f"GEPA source differs from pinned reference {REVISION}; install with: {INSTALL}")
    return digest


def load_reference():
    spec = importlib.util.find_spec("gepa")
    require(spec is not None and spec.origin is not None,
            f"optional pinned GEPA runtime is missing; install with: {INSTALL}")
    root = Path(spec.origin).resolve().parent
    verify_source(root)
    return importlib.import_module("gepa")
