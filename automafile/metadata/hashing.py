"""Centralized SHA-256 helpers."""

from __future__ import annotations

import hashlib
from pathlib import Path


CHUNK = 65536
HASH_PREFIX = "sha256:"


def hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return HASH_PREFIX + h.hexdigest()
