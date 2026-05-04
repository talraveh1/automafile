"""Helpers for walking the documents tree without entering blocked subtrees."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from dragndoc.config import get_settings


def directory_has_blocking_meta_file(directory: Path) -> bool:
    meta_name = get_settings().meta_subfolder
    return (directory / meta_name).is_file()


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
    meta_name = get_settings().meta_subfolder
    stack: list[Path] = [root]

    while stack:
        current = stack.pop()
        if current != root and current.name.startswith("."):
            continue
        if (current / meta_name).is_file():
            # Later we may classify and file this directory as a single unit.
            # For now we skip the whole subtree because a `.meta` file blocks sidecar writes here.
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