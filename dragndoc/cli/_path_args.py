"""Shared CLI path-argument expander: file, directory, or glob → list of paths.

Used by `dnd digest`, `dnd rm`, `dnd meta *`, `dnd transcript *`, `dnd ocr`,
`dnd mv`, `dnd cp`. Handles three shell realities at once:

- Bash already expanded the glob before Python saw it — multiple positional
  args arrive, each is a single file path.
- PowerShell didn't expand — one positional arg with `*`/`?` in it.
- The user typed a directory (a flat list of its files), or a directory plus
  ``--recursive`` (its whole tree).

The function returns deduplicated, sorted, existing files. Callers add
their own kind filter (audio/video, audio-only, etc.) via ``kinds``.
"""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path
from typing import Iterable, Sequence


# characters that mark an arg as a glob pattern (Path.glob territory)
_GLOB_CHARS = ("*", "?", "[")


def _is_glob(arg: str) -> bool:
    return any(c in arg for c in _GLOB_CHARS)


def _split_glob_root(pattern: str) -> tuple[Path, str]:
    """Split a glob pattern into a non-glob base directory + a glob suffix.

    Path.glob() needs to be called on the base directory (with the suffix as
    its argument), so we walk up the path components until we find the first
    one that contains a glob character.
    """
    p = Path(pattern)
    parts = p.parts
    glob_parts: list[str] = []
    base_parts: list[str] = []
    in_glob = False
    for part in parts:
        if in_glob or _is_glob(part):
            in_glob = True
            glob_parts.append(part)
        else:
            base_parts.append(part)
    base = Path(*base_parts) if base_parts else Path(".")
    suffix = os.sep.join(glob_parts).replace(os.sep, "/") if glob_parts else ""
    return base, suffix


def _walk_directory(
    root: Path,
    *,
    recursive: bool,
    insensitive: bool,
) -> list[Path]:
    """Return files under ``root``. ``recursive`` enables rglob; insensitive
    is informational only here (case-sensitivity is up to the filesystem)."""
    if not root.exists() or not root.is_dir():
        return []
    iterator = root.rglob("*") if recursive else root.iterdir()
    return [p for p in iterator if p.is_file()]


def _expand_glob_pattern(
    pattern: str,
    *,
    insensitive: bool,
) -> list[Path]:
    """Expand a glob pattern into a list of matching files. ``insensitive``
    uses fnmatch over a walked tree (Path.glob is always case-sensitive on
    POSIX, case-insensitive on Windows — fnmatch lets us be consistent)."""
    p = Path(pattern)
    if insensitive:
        # walk from the longest non-glob prefix, then fnmatch-filter
        base, suffix = _split_glob_root(pattern)
        if not suffix:
            # no glob chars after all — treat as plain file/dir
            return [base] if base.is_file() else _walk_directory(base, recursive=True, insensitive=True)
        if not base.exists() or not base.is_dir():
            return []
        # walk the whole base tree, fnmatch each path's portion below base
        out: list[Path] = []
        suffix_lower = suffix.lower()
        for entry in base.rglob("*"):
            if not entry.is_file():
                continue
            try:
                rel = entry.relative_to(base).as_posix()
            except ValueError:
                continue
            if fnmatch.fnmatchcase(rel.lower(), suffix_lower):
                out.append(entry)
        return out
    # case-sensitive (default): use Path.glob/rglob
    if p.is_absolute():
        # Path.glob doesn't handle absolute patterns directly; split base + relative
        base, suffix = _split_glob_root(pattern)
        if not suffix:
            return [base] if base.is_file() else _walk_directory(base, recursive=True, insensitive=False)
        if not base.exists() or not base.is_dir():
            return []
        try:
            return [m for m in base.glob(suffix) if m.is_file()]
        except (OSError, ValueError):
            return []
    # relative pattern: use Path(".").glob
    try:
        return [m for m in Path(".").glob(pattern) if m.is_file()]
    except (OSError, ValueError):
        return []


def expand_paths(
    args: Sequence[Path | str],
    *,
    recursive: bool = False,
    insensitive: bool = False,
    kinds: set[str] | None = None,
    must_exist: bool = True,
) -> list[Path]:
    """Expand CLI path arguments into a sorted, deduplicated list of files.

    ``args``: zero or more strings / Paths. Each can be:
      - a single file path → kept as-is
      - a directory → flat listing (or rglob if ``recursive``)
      - a glob pattern (contains ``*`` / ``?`` / ``[``) → expanded via Path.glob,
        with optional case-insensitive matching via ``insensitive``

    ``recursive``: when an arg is a directory, walk its whole tree.
    ``insensitive``: case-insensitive glob and directory matching (the latter
        is filesystem-dependent; the flag's most consistent effect is on
        the glob patterns themselves).
    ``kinds``: optional set of lowercase suffixes (``{".mp3", ".amr"}``) — when
        provided, only files with one of those suffixes are returned.
    ``must_exist``: when True (default), silently drop args that don't resolve
        to existing files. When False, return unresolved paths so callers can
        emit per-arg error messages.

    Bash-expanded multi-arg and PowerShell-single-arg invocations both work:
    ``dnd digest a.mp3 b.mp3 c.mp3`` and ``dnd digest 'Inbox/**/*.mp3'`` produce
    equivalent inputs to this expander.
    """
    out: list[Path] = []

    for raw in args:
        s = str(raw)
        if _is_glob(s):
            matches = _expand_glob_pattern(s, insensitive=insensitive)
            out.extend(matches)
            continue
        p = Path(s)
        if p.is_dir():
            out.extend(_walk_directory(p, recursive=recursive, insensitive=insensitive))
            continue
        if p.is_file():
            out.append(p)
            continue
        if not must_exist:
            out.append(p)

    if kinds is not None:
        suffix_set = {s.lower() for s in kinds}
        out = [p for p in out if p.suffix.lower() in suffix_set]

    # dedupe + stable order
    seen: set[Path] = set()
    unique: list[Path] = []
    for p in out:
        try:
            resolved = p.resolve()
        except OSError:
            resolved = p
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(p)
    unique.sort(key=lambda x: str(x).lower())
    return unique


def is_pattern_arg(arg: str | Path) -> bool:
    """True when ``arg`` looks like a glob or a directory (i.e., expander returns >1 file)."""
    s = str(arg)
    if _is_glob(s):
        return True
    p = Path(s)
    return p.is_dir()
