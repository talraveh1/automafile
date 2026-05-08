"""Triage queue: docs that have been digested and are awaiting filing.

Filled by the pipeline after a successful ``digest_file``; drained by the
``/triage`` skill via ``dnd triage next`` / ``dnd triage done``. By default
queries are scoped to the inbox so the skill only files newly-arrived
documents; ``--all`` (``inbox_only=False``) widens to anything in the queue
(used when reorganising existing files after a taxonomy change).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from dragndoc.config import get_settings
from dragndoc.db import connect, transaction
from dragndoc.log import get_logger
from dragndoc.meta_store import Doc, _row_to_doc, utc_now_iso


log = get_logger(__name__)


@dataclass
class QueueEntry:
    doc: Doc
    enqueued_at: str
    reason: str


def _inbox_prefix() -> str:
    return get_settings().inbox.rstrip("/") + "/"


def enqueue(doc_id: int, reason: str = "digested") -> None:
    """Add (or refresh) a row's place in the queue."""
    with transaction() as conn:
        conn.execute(
            "INSERT INTO triage (doc_id, enqueued_at, reason) VALUES (?, ?, ?) "
            "ON CONFLICT(doc_id) DO UPDATE SET enqueued_at = excluded.enqueued_at, "
            "reason = excluded.reason",
            (doc_id, utc_now_iso(), reason),
        )


def dequeue_by_doc_id(doc_id: int) -> bool:
    with transaction() as conn:
        cur = conn.execute("DELETE FROM triage WHERE doc_id = ?", (doc_id,))
    return cur.rowcount > 0


def dequeue_by_path(rel_path: str) -> bool:
    with transaction() as conn:
        cur = conn.execute(
            "DELETE FROM triage WHERE doc_id = (SELECT id FROM docs WHERE path = ?)",
            (rel_path,),
        )
    return cur.rowcount > 0


def _select_real(*, inbox_only: bool) -> list[QueueEntry]:
    sql = (
        "SELECT q.enqueued_at AS q_enqueued_at, q.reason AS q_reason, d.* "
        "FROM triage q JOIN docs_full d ON d.id = q.doc_id"
    )
    params: list[Any] = []
    if inbox_only:
        sql += " WHERE d.path LIKE ?"
        params.append(f"{_inbox_prefix()}%")
    sql += " ORDER BY q.enqueued_at ASC, q.doc_id ASC"
    with connect(readonly=True) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [QueueEntry(doc=_row_to_doc(r), enqueued_at=r["q_enqueued_at"], reason=r["q_reason"]) for r in rows]


def _select_synthetic_dups(*, inbox_only: bool) -> list[QueueEntry]:
    sql = (
        "SELECT d.* FROM docs_full d "
        "WHERE d.dup = 'dup' "
        "AND NOT EXISTS (SELECT 1 FROM triage q WHERE q.doc_id = d.id)"
    )
    params: list[Any] = []
    if inbox_only:
        sql += " AND d.path LIKE ?"
        params.append(f"{_inbox_prefix()}%")
    sql += " ORDER BY COALESCE(d.digested, d.path) ASC, d.id ASC"
    with connect(readonly=True) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [
        QueueEntry(doc=_row_to_doc(r), enqueued_at=r["digested"] or "", reason="duplicate")
        for r in rows
    ]


def _select(*, inbox_only: bool, limit: int | None) -> list[QueueEntry]:
    out = _select_real(inbox_only=inbox_only) + _select_synthetic_dups(inbox_only=inbox_only)
    out.sort(key=lambda entry: (entry.enqueued_at or "", entry.doc.id or 0))
    return out[:limit] if limit is not None else out


def list_queue(*, inbox_only: bool = True) -> list[QueueEntry]:
    return _select(inbox_only=inbox_only, limit=None)


def next_entry(*, inbox_only: bool = True) -> QueueEntry | None:
    entries = _select(inbox_only=inbox_only, limit=1)
    return entries[0] if entries else None


def count(*, inbox_only: bool = True) -> int:
    return len(_select(inbox_only=inbox_only, limit=None))


def clear(*, inbox_only: bool = True) -> int:
    """Drop entries from the queue. Returns the number removed."""
    if inbox_only:
        sql = (
            "DELETE FROM triage WHERE doc_id IN ("
            "SELECT id FROM docs WHERE path LIKE ?)"
        )
        params: list[Any] = [f"{_inbox_prefix()}%"]
    else:
        sql = "DELETE FROM triage"
        params = []
    with transaction() as conn:
        cur = conn.execute(sql, params)
    return cur.rowcount


def rebuild_from_existing_docs(*, inbox_only: bool = True) -> int:
    """Seed the queue with every doc that has a row but isn't already queued.

    One-shot migration aid for installs that pre-date the queue: enqueues every
    inbox row (or every row, with ``inbox_only=False``) at ``utc_now_iso()``.
    Already-queued rows are left alone.
    """
    sql = (
        "INSERT INTO triage (doc_id, enqueued_at, reason) "
        "SELECT d.id, ?, 'rebuild' FROM docs d "
        "WHERE NOT EXISTS (SELECT 1 FROM triage q WHERE q.doc_id = d.id)"
    )
    params: list[Any] = [utc_now_iso()]
    if inbox_only:
        sql += " AND d.path LIKE ?"
        params.append(f"{_inbox_prefix()}%")
    with transaction() as conn:
        cur = conn.execute(sql, params)
    return cur.rowcount
