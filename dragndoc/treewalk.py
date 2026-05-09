"""Helpers for walking the documents tree without entering blocked subtrees.

A user can drop a file literally named ``.meta`` into a directory to mark
that whole subtree as "don't process" — useful for bundle directories or
private folders. The marker is a *file*, not the legacy sidecar
*directory* (which has been deleted in the DB-based layout).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from dragndoc.dirs import OPAQUE_DIR_NAMES, get_dir


BLOCK_MARKER_FILENAME = ".meta"


def directory_has_blocking_meta_file(directory: Path) -> bool:
    return (directory / BLOCK_MARKER_FILENAME).is_file()


def directory_is_opaque(directory: Path) -> bool:
    row = get_dir(directory)
    if row is not None:
        return row.mode == "opaque"
    return directory.name.lower() in OPAQUE_DIR_NAMES


def is_in_blocked_subtree(path: Path, *, stop_at: Path | None = None) -> bool:
    current = path if path.is_dir() else path.parent
    stop = stop_at.resolve() if stop_at is not None else None

    while True:
        if directory_has_blocking_meta_file(current):
            return True
        if stop is not None and current.resolve() == stop:
            return False
        parent = current.parent
        if parent == current:
            return False
        current = parent


def iter_unblocked_directories(root: Path) -> Iterator[Path]:
    stack: list[Path] = [root]

    while stack:
        current = stack.pop()
        if current != root and current.name.startswith("."):
            continue
        if directory_has_blocking_meta_file(current):
            # A `.meta` marker file blocks processing of this subtree entirely.
            continue
        if current != root and directory_is_opaque(current):
            continue

        yield current

        try:
            child_dirs = sorted(
                (entry for entry in current.iterdir() if entry.is_dir()),
                key=lambda path: path.name.lower(),
            )
        except OSError:
            continue

        for child in reversed(child_dirs):
            if child.name.startswith("."):
                continue
            stack.append(child)


def iter_unblocked_files(root: Path) -> Iterator[Path]:
    for directory in iter_unblocked_directories(root):
        try:
            files = sorted(
                (entry for entry in directory.iterdir() if entry.is_file()),
                key=lambda path: path.name.lower(),
            )
        except OSError:
            continue

        for path in files:
            if path.name.startswith("."):
                continue
            yield path
