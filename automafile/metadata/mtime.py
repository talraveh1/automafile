"""Snapshot and restore filesystem mtime/atime around metadata writes."""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path


def snapshot(path: Path) -> tuple[int, int]:
    """Return ``(atime_ns, mtime_ns)`` for the file."""
    st = path.stat()
    return (st.st_atime_ns, st.st_mtime_ns)


def restore(path: Path, snap: tuple[int, int]) -> None:
    os.utime(path, ns=snap)


@contextmanager
def preserve_times(path: Path):
    """Context manager: capture atime/mtime, restore after the body runs."""
    snap = snapshot(path)
    try:
        yield
    finally:
        try:
            restore(path, snap)
        except FileNotFoundError:
            pass
