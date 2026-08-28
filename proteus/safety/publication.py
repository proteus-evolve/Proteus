"""Atomic publication for controller-owned safety artifacts."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path


class AtomicSafetyPublication:
    """Publish one complete safety artifact tree with an atomic rename."""

    def __init__(self, final_root: Path, *, label: str = "safety artifact") -> None:
        self.final_root = Path(final_root)
        self._label = label
        self.staging_root: Path | None = None
        self._published = False

    def __enter__(self) -> AtomicSafetyPublication:  # noqa: PYI034 - Python 3.10 support
        if self.final_root.exists():
            raise FileExistsError(f"{self._label} already exists: {self.final_root}")
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


class AtomicGatePublication(AtomicSafetyPublication):
    """Retain the publication name consumed by the legacy activation gate."""

    def __init__(self, final_root: Path, *, label: str = "safety gate") -> None:
        super().__init__(final_root, label=label)


class AtomicRetrospectivePublication(AtomicSafetyPublication):
    """Publish one immutable-snapshot replay without sharing a gate namespace."""

    def __init__(self, final_root: Path) -> None:
        super().__init__(final_root, label="retrospective safety artifact")


def json_value(value):
    """Turn typed safety evidence into the terminal JSON artifact representation."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return {key: json_value(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_value(item) for item in value]
    return value


def write_json(path: Path, value) -> None:
    """Atomically write a typed terminal artifact below an active publication."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(json_value(value), indent=1, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def write_jsonl(path: Path, values) -> None:
    """Atomically write complete typed records below an active publication."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        "".join(
            json.dumps(json_value(value), sort_keys=True) + "\n" for value in values
        ),
        encoding="utf-8",
    )
    temporary.replace(path)
