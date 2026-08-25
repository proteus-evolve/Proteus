"""Atomic publication for one activation-safety gate attempt."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Self


class AtomicGatePublication:
    def __init__(self, final_root: Path) -> None:
        self.final_root = Path(final_root)
        self.staging_root: Path | None = None
        self._published = False

    def __enter__(self) -> Self:
        if self.final_root.exists():
            raise FileExistsError(f"safety gate already exists: {self.final_root}")
        parent = self.final_root.parent
        parent.mkdir(parents=True, exist_ok=True)
        self.staging_root = Path(
            tempfile.mkdtemp(prefix=f".{self.final_root.name}-", dir=parent)
        )
        return self

    def publish(self) -> None:
        if self.staging_root is None:
            raise RuntimeError("safety publication has not started")
        if self._published:
            raise RuntimeError("safety publication is already complete")
        os.replace(self.staging_root, self.final_root)
        self._published = True

    def __exit__(self, exc_type, exc, traceback) -> bool:
        del exc_type, traceback
        if exc is not None and self.staging_root is not None and self.staging_root.exists():
            failed = self.final_root.parent / ".failed"
            failed.mkdir(parents=True, exist_ok=True)
            destination = failed / self.staging_root.name.removeprefix(".")
            os.replace(self.staging_root, destination)
        return False
