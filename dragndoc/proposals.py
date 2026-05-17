"""Shared proposals surface: every pending user decision lands in one table.

A *proposal* is a value the system would commit if the user approves —
recording-type guesses, speaker-name suggestions from path patterns,
folder-mode classifications from the LLM, etc. They live in a single
``proposals`` table walked by ``dnd review``, so consumers (the audio
extractor, the dir classifier, the speakers CLI) don't each grow their
own queue / column / worklist bucket.

Subject keys are ``"doc:<id>"`` for per-document proposals and
``"dir:<rel-path>"`` for per-directory ones. The ``value`` column is
JSON; the schema varies by ``kind`` and is enforced at the call site.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Iterable

from dragndoc.db import connect, transaction
from dragndoc.log import get_logger
from dragndoc.meta_store import utc_now_iso_micro


log = get_logger(__name__)


# proposal kinds we currently emit (extensible — store any string)
KIND_RECORDING_TYPE = "recording_type"
KIND_SPEAKER_NAME = "speaker_name"
KIND_DIR_MODE = "dir_mode"


@dataclass
class Proposal:
    id: int | None = None
    subject: str = ""
    kind: str = ""
    value: dict[str, Any] = field(default_factory=dict)
    source: str = ""             # "path_pattern" | "channel" | "llm" | "heuristic" | "fallback"
    rationale: str | None = None
    created_at: str = ""
    status: str = "pending"

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Proposal":
        try:
            value = json.loads(row["value"])
        except (TypeError, json.JSONDecodeError):
            value = {}
        return cls(
            id=int(row["id"]),
            subject=str(row["subject"]),
            kind=str(row["kind"]),
            value=value if isinstance(value, dict) else {"_raw": value},
            source=str(row["source"]),
            rationale=row["rationale"] if "rationale" in row.keys() else None,
            created_at=str(row["created_at"]),
            status=str(row["status"]),
        )


# ---------------------------------------------------------------------------
# subject helpers
# ---------------------------------------------------------------------------


def subject_for_doc(doc_id: int) -> str:
    return f"doc:{int(doc_id)}"


def subject_for_dir(rel_path: str) -> str:
    return f"dir:{rel_path}"


# ---------------------------------------------------------------------------
# enqueue
# ---------------------------------------------------------------------------


def enqueue(
    *,
    subject: str,
    kind: str,
    value: dict[str, Any],
    source: str,
    rationale: str | None = None,
    supersede_existing: bool = True,
) -> int:
    """Insert a new pending proposal. Returns the new row id.

    When ``supersede_existing`` is True (default), any *pending* proposal
    with the same ``(subject, kind)`` is marked ``superseded`` first so
    only one row is live at a time for a given subject+kind combo.
    """
    with transaction() as conn:
        if supersede_existing:
            conn.execute(
                "UPDATE proposals SET status = 'superseded' "
                "WHERE subject = ? AND kind = ? AND status = 'pending'",
                (subject, kind),
            )
        cur = conn.execute(
            "INSERT INTO proposals (subject, kind, value, source, rationale, created_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending')",
            (
                subject, kind,
                json.dumps(value, ensure_ascii=False),
                source, rationale,
                utc_now_iso_micro(),
            ),
        )
        new_id = cur.lastrowid
    log.info(
        "Proposal enqueued: subject=%s kind=%s source=%s id=%s",
        subject, kind, source, new_id,
    )
    return int(new_id) if new_id is not None else 0


def enqueue_many(items: Iterable[Proposal]) -> int:
    """Bulk-enqueue. Each item's id/created_at are ignored; status forced to pending."""
    count = 0
    for p in items:
        enqueue(
            subject=p.subject,
            kind=p.kind,
            value=p.value,
            source=p.source,
            rationale=p.rationale,
        )
        count += 1
    return count


# ---------------------------------------------------------------------------
# query
# ---------------------------------------------------------------------------


def list_pending(
    *,
    kind: str | None = None,
    subject: str | None = None,
    limit: int | None = None,
) -> list[Proposal]:
    sql = "SELECT * FROM proposals WHERE status = 'pending'"
    params: list[Any] = []
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    if subject:
        sql += " AND subject = ?"
        params.append(subject)
    sql += " ORDER BY id"
    if limit is not None and limit > 0:
        sql += " LIMIT ?"
        params.append(int(limit))
    with connect(readonly=True) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [Proposal.from_row(r) for r in rows]


def get(proposal_id: int) -> Proposal | None:
    with connect(readonly=True) as conn:
        row = conn.execute("SELECT * FROM proposals WHERE id = ?", (proposal_id,)).fetchone()
    return Proposal.from_row(row) if row else None


def count_pending(kind: str | None = None) -> int:
    sql = "SELECT COUNT(*) AS n FROM proposals WHERE status = 'pending'"
    params: list[Any] = []
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    with connect(readonly=True) as conn:
        row = conn.execute(sql, params).fetchone()
    return int(row["n"])


# ---------------------------------------------------------------------------
# status transitions
# ---------------------------------------------------------------------------


def accept(proposal_id: int) -> Proposal | None:
    """Mark a proposal as accepted. Returns the updated row, or None if missing."""
    with transaction() as conn:
        conn.execute("UPDATE proposals SET status = 'accepted' WHERE id = ?", (proposal_id,))
    return get(proposal_id)


def reject(proposal_id: int) -> Proposal | None:
    with transaction() as conn:
        conn.execute("UPDATE proposals SET status = 'rejected' WHERE id = ?", (proposal_id,))
    return get(proposal_id)


def update_value(proposal_id: int, value: dict[str, Any]) -> Proposal | None:
    """Replace a pending proposal's value (used when the user edits before accepting)."""
    with transaction() as conn:
        conn.execute(
            "UPDATE proposals SET value = ? WHERE id = ?",
            (json.dumps(value, ensure_ascii=False), proposal_id),
        )
    return get(proposal_id)
