"""A host-side single-writer lease; neither snapshot rollback nor seeding removes it."""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path


def _lock(handle, *, acquire: bool) -> None:
    if os.name == "nt":
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK if acquire else msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB if acquire else fcntl.LOCK_UN)


@contextmanager
def run_lock(root: Path):
    root = Path(root).resolve()
    directory = root.parent / ".proteus-locks"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (hashlib.sha256(str(root).encode()).hexdigest() + ".lock")
    with path.open("a+b") as handle:
        if os.name == "nt":
            if path.stat().st_size == 0:
                handle.write(b"\0")
                handle.flush()
        try:
            _lock(handle, acquire=True)
        except OSError as exc:
            raise ValueError(f"another controller owns run {root}") from exc
        try:
            yield
        finally:
            _lock(handle, acquire=False)
