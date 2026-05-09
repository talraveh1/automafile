"""Path normalization helpers shared by DB-backed metadata stores."""

from __future__ import annotations

import os
from pathlib import Path


Pathish = str | os.PathLike[str]


def normalize(value: Pathish, *, root: Path | None = None) -> str:
    """Return a stable DB path: forward slashes, no trailing slash."""
    raw: str
    if isinstance(value, os.PathLike):
        path = Path(value).expanduser()
        if root is not None:
            try:
                raw = str(path.resolve().relative_to(root.expanduser().resolve()))
            except (OSError, ValueError):
                raw = str(path)
        else:
            raw = str(path)
    else:
        raw = value

    normalized = raw.strip().replace("\\", "/")
    if len(normalized) >= 2 and normalized[1] == ":":
        normalized = normalized[0].lower() + normalized[1:]
    while len(normalized) > 1 and normalized.endswith("/"):
        normalized = normalized[:-1]
    if normalized == ".":
        return ""
    return normalized


def like_child_pattern(path: str) -> str:
    """Return a LIKE pattern matching immediate or deep children of ``path``."""
    escaped = path.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"{escaped}/%"
