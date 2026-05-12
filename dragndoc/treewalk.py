"""Helpers for walking the documents tree without entering opaque subtrees.

A directory is opaque if any of these apply:

- its name starts with ``.`` (e.g. ``.venv``, ``.git``)
- its lowercased name is in :data:`OPAQUE_DIR_NAMES` (e.g. ``node_modules``)
- the ``dirs`` table records it with ``mode = 'opaque'`` (set via
  ``dnd dirs set <path> opaque``)
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from dragndoc.dirs import OPAQUE_DIR_NAMES, get_dir


def directory_is_opaque(directory: Path) -> bool:
    if directory.name.startswith("."):
        return True
    if directory.name.lower() in OPAQUE_DIR_NAMES:
        return True
    row = get_dir(directory)
    return row is not None and row.mode == "opaque"


def is_in_opaque_subtree(path: Path, *, stop_at: Path | None = None) -> bool:
    current = path if path.is_dir() else path.parent
    stop = stop_at.resolve() if stop_at is not None else None

    while True:
        if stop is not None and current.resolve() == stop:
            return False
        if directory_is_opaque(current):
            return True
        parent = current.parent
        if parent == current:
            return False
        current = parent


def iter_unblocked_directories(root: Path) -> Iterator[Path]:
    stack: list[Path] = [root]

    while stack:
        current = stack.pop()
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
