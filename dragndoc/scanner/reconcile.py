"""Single-file and path-conflict reconciliation helpers."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dragndoc.config import get_settings
from dragndoc.db import transaction
from dragndoc.meta_store import (
    Doc,
    _recompute_dups_for_hashes,
    _row_to_doc,
    file_modified_iso,
    relative_to_root,
)
from dragndoc.metadata.hashing import hash_file


CONFIDENCE_RANK = {"confirmed": 3, "high": 2, "medium": 1, "low": 0}
DUP_RANK = {"keep": 2, "dup": 1, "unique": 0}


@dataclass
class Renamed:
    row: Doc
    file_hash: str


@dataclass
class ContentChanged:
    row: Doc
    file_hash: str


@dataclass
class NewFile:
    file_hash: str


@dataclass
class NoChange:
    pass


ReconcileOutcome = Renamed | ContentChanged | NewFile | NoChange


def _full_path(rel: str) -> Path:
    return get_settings().docs / rel


def _richness_score(row: sqlite3.Row) -> tuple[int, int, int, str]:
    filled = 0
    for field in ("title", "summary"):
        if row[field]:
            filled += 1
    if row["tags"]:
        filled += len([chunk for chunk in str(row["tags"]).strip(";").split(";") if chunk])
    # prefer rows that already carry stronger duplicate decisions and richer metadata
    return (
        DUP_RANK.get(str(row["dup"]), 0),
        CONFIDENCE_RANK.get(str(row["confidence"]), 0),
        filled,
        str(row["digested"] or ""),
    )


def _pick_winner(a: sqlite3.Row, b: sqlite3.Row) -> tuple[sqlite3.Row, sqlite3.Row]:
    a_score = _richness_score(a)
    b_score = _richness_score(b)
    if a_score != b_score:
        return (a, b) if a_score > b_score else (b, a)
    # break ties deterministically so repeated scans converge on the same surviving row
    return (a, b) if str(a["path"]) <= str(b["path"]) else (b, a)


def _merge_semilist(a: str | None, b: str | None) -> str:
    values = set()
    for raw in (a or "", b or ""):
        values.update(chunk for chunk in raw.strip(";").split(";") if chunk)
    return ";" + ";".join(sorted(values)) + ";" if values else ""


def _merged_doc_values(winner: sqlite3.Row, loser: sqlite3.Row) -> dict[str, Any]:
    values = dict(winner)
    for field in ("category", "date", "title", "summary", "notes", "extra"):
        if not values.get(field) and loser[field]:
            values[field] = loser[field]
    for field in ("parties", "langs", "tags"):
        values[field] = _merge_semilist(values.get(field), loser[field])
    return values


def resolve_path_conflict(
    conn: sqlite3.Connection,
    *,
    old_row: sqlite3.Row,
    new_row: sqlite3.Row,
    new_path: str,
    size: int,
    modified: str | None,
) -> tuple[int, int] | None:
    if old_row["hash"] != new_row["hash"]:
        return None

    # same content at the target path can be merged into the richer surviving row
    winner, loser = _pick_winner(old_row, new_row)
    winner_values = _merged_doc_values(winner, loser)
    loser_ocr = conn.execute("SELECT * FROM ocr WHERE doc_id = ?", (loser["id"],)).fetchone()
    winner_ocr = conn.execute("SELECT 1 FROM ocr WHERE doc_id = ?", (winner["id"],)).fetchone()
    if loser_ocr is not None and winner_ocr is None:
        # preserve the only OCR payload by reattaching it to the surviving doc row
        conn.execute("UPDATE ocr SET doc_id = ? WHERE doc_id = ?", (winner["id"], loser["id"]))

    conn.execute("DELETE FROM docs WHERE id = ?", (loser["id"],))
    conn.execute(
        "UPDATE docs SET path = ?, size = ?, modified = ?, category = ?, parties = ?, "
        "langs = ?, tags = ?, date = ?, title = ?, confidence = ?, dup = ?, "
        "summary = ?, notes = ?, extra = ? WHERE id = ?",
        (
            new_path,
            size,
            modified,
            winner_values["category"],
            winner_values["parties"],
            winner_values["langs"],
            winner_values["tags"],
            winner_values["date"],
            winner_values["title"],
            winner_values["confidence"],
            winner_values["dup"],
            winner_values["summary"],
            winner_values["notes"],
            winner_values["extra"],
            winner["id"],
        ),
    )
    return int(winner["id"]), int(loser["id"])


def reconcile_single(path: Path) -> ReconcileOutcome:
    rel = relative_to_root(path)
    file_hash = hash_file(path)
    st = path.stat()
    modified = file_modified_iso(path)
    with transaction() as conn:
        row = conn.execute("SELECT * FROM docs_full WHERE path = ?", (rel,)).fetchone()
        if row is not None:
            doc = _row_to_doc(row)
            if row["hash"] == file_hash:
                # refresh filesystem facts in place when content is unchanged but timestamps drift
                if row["size"] != st.st_size or row["modified"] != modified:
                    conn.execute(
                        "UPDATE docs SET size = ?, modified = ? WHERE id = ?",
                        (st.st_size, modified, row["id"]),
                    )
                return NoChange()
            return ContentChanged(doc, file_hash)

        rows = conn.execute("SELECT * FROM docs_full WHERE hash = ? ORDER BY path", (file_hash,)).fetchall()
        for candidate in rows:
            if not _full_path(candidate["path"]).exists():
                # reuse a stale row when the same file content has simply moved paths
                conn.execute(
                    "UPDATE docs SET path = ?, size = ?, modified = ? WHERE id = ?",
                    (rel, st.st_size, modified, candidate["id"]),
                )
                _recompute_dups_for_hashes(conn, [file_hash])
                renamed = conn.execute("SELECT * FROM docs_full WHERE id = ?", (candidate["id"],)).fetchone()
                return Renamed(_row_to_doc(renamed), file_hash)
    return NewFile(file_hash)
