"""Verified, atomic downloads for official benchmark datasets."""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path
from typing import Callable
from urllib import request


def download_verified(
    *, name: str, url: str, expected_sha256: str, cache: Path, validate: Callable[[Path], object]
) -> Path:
    """Download one official dataset, verify bytes and format, then publish atomically."""
    cache = Path(cache)
    if cache.exists():
        return cache
    cache.parent.mkdir(parents=True, exist_ok=True)
    with request.urlopen(url, timeout=30) as response:
        payload = response.read()
    actual = hashlib.sha256(payload).hexdigest()
    if actual != expected_sha256:
        raise ValueError(
            f"{name} checksum mismatch: expected {expected_sha256}, got {actual}"
        )
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=cache.parent, suffix="".join(cache.suffixes), delete=False
        ) as stream:
            stream.write(payload)
            temporary = Path(stream.name)
        validate(temporary)
        temporary.replace(cache)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return cache
