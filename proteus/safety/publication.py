"""Controller-owned atomic publication for one candidate gate attempt."""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Self

_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


def _validate_id(label: str, value: str) -> None:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise ValueError(f"{label} must be a safe path component")


def _atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as sink:
            sink.write(payload)
            sink.flush()
            os.fsync(sink.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


class AtomicCandidatePublication:
    """Stage a candidate tree, publish it by rename, and retain failed attempts."""

    def __init__(self, safety_root: Path, run_id: str, candidate_id: str) -> None:
        _validate_id("run ID", run_id)
        _validate_id("candidate ID", candidate_id)
        self.safety_root = Path(safety_root)
        self.run_id = run_id
        self.candidate_id = candidate_id
        self.run_root = self.safety_root / run_id
        self.final_root = self.run_root / candidate_id
        self.staging_root: Path | None = None
        self.failed_root: Path | None = None
        self._published = False

    def __enter__(self) -> Self:
        if self.final_root.exists():
            raise FileExistsError(f"candidate gate already published: {self.candidate_id}")
        staging_parent = self.run_root / ".staging"
        staging_parent.mkdir(parents=True, exist_ok=True)
        self.staging_root = Path(
            tempfile.mkdtemp(prefix=f"{self.candidate_id}-", dir=staging_parent)
        )
        return self

    def publish(self, *, activation: dict[str, object]) -> None:
        if self.staging_root is None:
            raise RuntimeError("candidate publication has not been entered")
        if self._published:
            raise RuntimeError("candidate publication is already complete")
        if self.final_root.exists():
            raise FileExistsError(f"candidate gate already published: {self.candidate_id}")
        os.replace(self.staging_root, self.final_root)
        _atomic_write(
            self.safety_root / "manifest.json",
            json.dumps(
                {"schema_version": "proteus-evolution-safety-gates/1"},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n",
        )
        activations = self.run_root / "activations.jsonl"
        existing = activations.read_text(encoding="utf-8") if activations.exists() else ""
        _atomic_write(
            activations,
            existing + json.dumps(activation, ensure_ascii=False, separators=(",", ":")) + "\n",
        )
        self._published = True

    def _retain_failure(self, exc: BaseException) -> None:
        source = self.final_root if self.final_root.exists() else self.staging_root
        if source is None or not source.exists():
            return
        failed_parent = self.run_root / ".failed"
        failed_parent.mkdir(parents=True, exist_ok=True)
        self.failed_root = Path(
            tempfile.mkdtemp(prefix=f"{self.candidate_id}-", dir=failed_parent)
        )
        self.failed_root.rmdir()
        os.replace(source, self.failed_root)
        failure = {
            "status": "error",
            "exception_type": type(exc).__name__,
            "message": "candidate gate publication failed",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        _atomic_write(
            self.failed_root / "failure.json",
            json.dumps(failure, ensure_ascii=False, separators=(",", ":")) + "\n",
        )

    def __exit__(self, exc_type, exc, traceback) -> bool:
        if exc is not None and not self._published:
            self._retain_failure(exc)
        return False
