"""Directory mode metadata and prefix operations."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dragndoc.config import get_settings
from dragndoc.db import connect, transaction
from dragndoc.paths import like_child_pattern, normalize


DIR_MODES = {"collection", "bundle", "opaque", "unknown"}
DIR_SOURCES = {"hardcoded", "heuristic", "user"}
OPAQUE_DIR_NAMES = {
    ".venv",
    "venv",
    "node_modules",
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    "build",
    "dist",
    "target",
    ".gradle",
    ".next",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".idea",
    ".vs",
    ".vscode",
    ".cache",
    ".ruff_cache",
}
MODE_TAGS = {"collection": "col", "bundle": "bun", "opaque": "opq", "unknown": "unk"}


@dataclass
class DirRow:
    path: str
    mode: str
    source: str
    decided_at: str
    fingerprint: str | None = None
    listing_id: str | None = None
    summary: str | None = None
    confidence: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "mode": self.mode,
            "source": self.source,
            "fingerprint": self.fingerprint,
            "listing_id": self.listing_id,
            "summary": self.summary,
            "decided_at": self.decided_at,
            "confidence": self.confidence,
        }


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def normalize_dir_path(path: str | Path) -> str:
    if isinstance(path, Path):
        settings = get_settings()
        return normalize(path, root=settings.docs) if path.is_absolute() else normalize(path)
    return normalize(path)


def auto_mode_for_name(name: str) -> tuple[str, str, float]:
    if name.lower() in OPAQUE_DIR_NAMES:
        return "opaque", "hardcoded", 1.0
    return "collection", "heuristic", 1.0


def auto_mode_for_path(path: Path) -> tuple[str, str, float]:
    return auto_mode_for_name(path.name)


def mode_tag(mode: str) -> str:
    return MODE_TAGS.get(mode, "unk")


def fingerprint_directory(path: Path) -> str | None:
    try:
        names = sorted((entry.name for entry in path.iterdir()), key=str.lower)
    except OSError:
        return None
    payload = json.dumps(names, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _row_to_dir(row: sqlite3.Row) -> DirRow:
    return DirRow(
        path=row["path"],
        mode=row["mode"],
        source=row["source"],
        fingerprint=row["fingerprint"],
        listing_id=row["listing_id"],
        summary=row["summary"],
        decided_at=row["decided_at"],
        confidence=row["confidence"],
    )


def get_dir(path: str | Path) -> DirRow | None:
    rel = normalize_dir_path(path)
    with connect(readonly=True) as conn:
        row = conn.execute("SELECT * FROM dirs WHERE path = ?", (rel,)).fetchone()
    return _row_to_dir(row) if row else None


def upsert_dir(row: DirRow) -> DirRow:
    if row.mode not in DIR_MODES:
        raise ValueError(f"Invalid directory mode: {row.mode}")
    if row.source not in DIR_SOURCES:
        raise ValueError(f"Invalid directory source: {row.source}")
    row.path = normalize(row.path)
    with transaction() as conn:
        conn.execute(
            """
            INSERT INTO dirs (
                path, mode, source, fingerprint, listing_id, summary, decided_at, confidence
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                mode = excluded.mode,
                source = excluded.source,
                fingerprint = excluded.fingerprint,
                listing_id = excluded.listing_id,
                summary = excluded.summary,
                decided_at = excluded.decided_at,
                confidence = excluded.confidence
            """,
            (
                row.path,
                row.mode,
                row.source,
                row.fingerprint,
                row.listing_id,
                row.summary,
                row.decided_at,
                row.confidence,
            ),
        )
    return row


def ensure_tracked(path: Path) -> DirRow:
    rel = normalize_dir_path(path)
    existing = get_dir(rel)
    fingerprint = fingerprint_directory(path) if path.exists() and path.is_dir() else None
    if existing is not None and existing.source == "user":
        with transaction() as conn:
            conn.execute("UPDATE dirs SET fingerprint = ? WHERE path = ?", (fingerprint, rel))
        refreshed = get_dir(rel)
        return refreshed if refreshed is not None else existing

    mode, source, confidence = auto_mode_for_path(path)
    return upsert_dir(
        DirRow(
            path=rel,
            mode=mode,
            source=source,
            fingerprint=fingerprint,
            listing_id=existing.listing_id if existing else None,
            summary=existing.summary if existing else None,
            decided_at=utc_now_iso(),
            confidence=confidence,
        )
    )


def observe_tree(root: Path, *, include_root: bool = False) -> int:
    observed = 0
    stack: list[Path] = [root]
    while stack:
        current = stack.pop()
        row: DirRow | None = None
        if include_root or current != root:
            row = ensure_tracked(current)
            observed += 1
        if row is not None and row.mode == "opaque":
            continue
        try:
            child_dirs = sorted(
                (entry for entry in current.iterdir() if entry.is_dir()),
                key=lambda path: path.name.lower(),
            )
        except OSError:
            continue
        for child in reversed(child_dirs):
            stack.append(child)
    return observed


def set_mode(path: str | Path, mode: str) -> DirRow:
    if mode not in DIR_MODES - {"unknown"}:
        raise ValueError(f"Invalid directory mode: {mode}")
    rel = normalize_dir_path(path)
    settings = get_settings()
    filesystem_path = path if isinstance(path, Path) and path.is_absolute() else settings.docs / rel
    fingerprint = fingerprint_directory(filesystem_path) if filesystem_path.exists() else None
    existing = get_dir(rel)
    return upsert_dir(
        DirRow(
            path=rel,
            mode=mode,
            source="user",
            fingerprint=fingerprint,
            listing_id=existing.listing_id if existing else None,
            summary=existing.summary if existing else None,
            decided_at=utc_now_iso(),
            confidence=None,
        )
    )


def list_dirs(parent: str | Path | None = None) -> list[DirRow]:
    rel = normalize_dir_path(parent) if parent is not None else ""
    with connect(readonly=True) as conn:
        if not rel:
            rows = conn.execute("SELECT * FROM dirs ORDER BY path").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM dirs WHERE path = ? OR path LIKE ? ESCAPE '\\' ORDER BY path",
                (rel, like_child_pattern(rel)),
            ).fetchall()
    return [_row_to_dir(row) for row in rows]


def count_prefix(conn: sqlite3.Connection, table: str, rel: str) -> int:
    if table not in {"docs", "dirs"}:
        raise ValueError(f"Invalid table for path-prefix count: {table}")
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM {table} WHERE path = ? OR path LIKE ? ESCAPE '\\'",
        (rel, like_child_pattern(rel)),
    ).fetchone()
    return int(row["n"] if isinstance(row, sqlite3.Row) else row[0])


def rewrite_prefix(conn: sqlite3.Connection, table: str, src: str, dst: str) -> int:
    if table not in {"docs", "dirs"}:
        raise ValueError(f"Invalid table for path-prefix rewrite: {table}")
    cur = conn.execute(
        f"""
        UPDATE {table}
        SET path = ? || SUBSTR(path, LENGTH(?) + 1)
        WHERE path = ? OR path LIKE ? ESCAPE '\\'
        """,
        (dst, src, src, like_child_pattern(src)),
    )
    return cur.rowcount


def delete_prefix(conn: sqlite3.Connection, table: str, rel: str) -> int:
    if table not in {"docs", "dirs"}:
        raise ValueError(f"Invalid table for path-prefix delete: {table}")
    cur = conn.execute(
        f"DELETE FROM {table} WHERE path = ? OR path LIKE ? ESCAPE '\\'",
        (rel, like_child_pattern(rel)),
    )
    return cur.rowcount
