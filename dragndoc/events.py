"""Event journal backed by the ``events`` table.

Decouples notifications from the pipeline: the watcher / pipeline writes
event rows here, and a separate ``dnd toaster`` process polls the table
and renders Windows toasts. The cursor file (``data/toaster.cursor``)
holds the highest event id the toaster has consumed.
"""

from __future__ import annotations

import json
from typing import Any

from dragndoc.db import transaction
from dragndoc.log import get_logger
from dragndoc.meta_store import utc_now_iso_micro


log = get_logger(__name__)


def append(kind: str, **fields: Any) -> None:
    """Append one event row. Best-effort — never raises; logs on failure."""
    payload = json.dumps(fields, ensure_ascii=False, separators=(",", ":"))
    try:
        with transaction() as conn:
            conn.execute(
                "INSERT INTO events (ts, kind, payload) VALUES (?, ?, ?)",
                (utc_now_iso_micro(), kind, payload),
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("events.append failed (%s): %s", kind, exc)


# Event kinds. Toaster reads these to drive both status and notifications.
DIGEST_STARTED = "digest_started"      # payload: {scope, count?, file?}
DIGEST_FINISHED = "digest_finished"    # payload: {scope, succeeded, failed, ready_count, file?, category?}
SCAN_STARTED = "scan_started"          # payload: {scope, path?}
SCAN_FINISHED = "scan_finished"        # payload: {seen, ready_count}
ERROR = "error"                        # payload: {file, error}


def fetch_since(last_id: int, *, limit: int = 500) -> list[dict[str, Any]]:
    """Read events with id > ``last_id`` in id-order.

    Returns a list of ``{id, ts, kind, payload}`` dicts where ``payload`` is
    already deserialized.
    """
    from dragndoc.db import connect

    out: list[dict[str, Any]] = []
    with connect(readonly=True) as conn:
        rows = conn.execute(
            "SELECT id, ts, kind, payload FROM events WHERE id > ? ORDER BY id LIMIT ?",
            (last_id, limit),
        ).fetchall()
    for r in rows:
        try:
            payload = json.loads(r["payload"]) if r["payload"] else {}
        except json.JSONDecodeError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        out.append({"id": r["id"], "ts": r["ts"], "kind": r["kind"], "payload": payload})
    return out


def latest_id() -> int:
    """Highest event id currently stored, or 0 if none."""
    from dragndoc.db import connect

    with connect(readonly=True) as conn:
        row = conn.execute("SELECT COALESCE(MAX(id), 0) AS mx FROM events").fetchone()
    return int(row["mx"] if row else 0)
