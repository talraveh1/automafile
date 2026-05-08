"""Events table + toaster cursor tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from dragndoc import events
from dragndoc.toaster import Cursor, _consume, _format_toast


def test_append_writes_row_with_ts_and_kind(docs_root):
    events.append("processed", file="foo.pdf", category="Receipts")
    rows = events.fetch_since(0)
    assert len(rows) == 1
    r = rows[0]
    assert r["kind"] == "processed"
    assert r["payload"]["file"] == "foo.pdf"
    assert r["payload"]["category"] == "Receipts"
    assert r["ts"].endswith("Z")


def test_append_multiple_events_preserves_order(docs_root):
    for i in range(5):
        events.append("processed", file=f"f{i}.pdf")
    rows = events.fetch_since(0)
    assert [r["payload"]["file"] for r in rows] == [f"f{i}.pdf" for i in range(5)]


def test_format_toast_processed():
    result = _format_toast({
        "kind": "processed",
        "payload": {"file": "f.pdf", "category": "Receipts", "target": "Receipts/2026-01 X.pdf"},
    })
    assert result is not None
    title, body = result
    assert title == "Drag'n'Doc"
    assert "f.pdf" in body
    assert "Receipts" in body


def test_format_toast_unknown_kind_falls_through():
    result = _format_toast({"kind": "weird", "payload": {"extra": "data"}})
    assert result is not None
    title, body = result
    assert title == "Drag'n'Doc"
    assert "weird" in body


def test_format_toast_error_uses_short_title():
    result = _format_toast({
        "kind": "error",
        "payload": {"file": "missing.pdf", "error": "FileNotFoundError"},
    })
    assert result is not None
    title, body = result
    assert title == "Error"
    assert "missing.pdf" in body
    assert "FileNotFoundError" in body


def test_format_toast_digest_finished_failure_uses_short_title():
    result = _format_toast({
        "kind": "digest_finished",
        "payload": {"failed": 1, "ready_count": 0, "file": "broken.pdf"},
    })
    assert result is not None
    title, body = result
    assert title == "Error"
    assert "broken.pdf" in body


def test_consume_mirrors_error_toast_to_log(docs_root, caplog):
    """Every user-facing toast must also land in the log file."""
    import logging

    events.append("error", file="missing.pdf", error="FileNotFoundError")
    notifier = MagicMock()
    with caplog.at_level(logging.ERROR, logger="dragndoc.toaster"):
        _consume(Cursor(), notifier)

    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert any("missing.pdf" in r.getMessage() for r in error_records)


def test_consume_mirrors_info_toast_to_log(docs_root, caplog):
    import logging

    events.append("processed", file="a.pdf", category="Receipts")
    notifier = MagicMock()
    with caplog.at_level(logging.INFO, logger="dragndoc.toaster"):
        _consume(Cursor(), notifier)

    info_records = [r for r in caplog.records if r.levelno == logging.INFO]
    assert any("a.pdf" in r.getMessage() for r in info_records)


def test_consume_logs_even_when_muted(docs_root, caplog):
    """Muting the toast should not mute the log entry."""
    import logging
    from dragndoc.toaster import TrayState

    state = TrayState()
    state.toggle_notifications()  # → disabled
    assert not state.is_enabled()

    events.append("error", file="x.pdf", error="boom")
    notifier = MagicMock()
    with caplog.at_level(logging.ERROR, logger="dragndoc.toaster"):
        _consume(Cursor(), notifier, state)

    notifier.notify.assert_not_called()
    assert any("x.pdf" in r.getMessage() and r.levelno == logging.ERROR for r in caplog.records)


def test_consume_advances_cursor_and_fires_toast(docs_root):
    events.append("processed", file="a.pdf", category="X")
    events.append("processed", file="b.pdf", category="Y")

    notifier = MagicMock()
    cursor = _consume(Cursor(), notifier)

    assert notifier.notify.call_count == 2
    assert cursor.last_id > 0


def test_consume_resumes_from_saved_cursor(docs_root):
    events.append("processed", file="a.pdf")
    notifier = MagicMock()
    cursor = _consume(Cursor(), notifier)
    assert notifier.notify.call_count == 1

    events.append("processed", file="b.pdf")
    notifier2 = MagicMock()
    cursor = _consume(cursor, notifier2)
    assert notifier2.notify.call_count == 1
    assert "b.pdf" in notifier2.notify.call_args[0][1]


def test_mute_skips_toast_but_advances_cursor(docs_root):
    """Muted notifications still advance the cursor and update the status line."""
    from dragndoc.toaster import TrayState
    state = TrayState()
    state.toggle_notifications()  # → disabled
    assert not state.is_enabled()

    events.append("processed", file="a.pdf")
    events.append("processed", file="b.pdf")

    notifier = MagicMock()
    cursor = _consume(Cursor(), notifier, state)

    notifier.notify.assert_not_called()
    assert cursor.last_id > 0
    assert "b.pdf" in state.status_text()


def test_status_text_default_and_after_event(docs_root):
    from dragndoc.toaster import TrayState
    state = TrayState()
    assert state.status_text() == "No notifications yet"

    events.append("processed", file="report.pdf", category="Finance")
    _consume(Cursor(), MagicMock(), state)
    text = state.status_text()
    assert "report.pdf" in text
    assert "Finance" in text


def test_count_ready_for_triage(docs_root):
    """Files in the inbox count when they have real or synthetic triage entries."""
    from dragndoc.meta_store import Doc, OcrInfo, relative_to_root, upsert
    from dragndoc.metadata.hashing import hash_file
    from dragndoc.toaster import _count_ready_for_triage
    from dragndoc.triage import enqueue

    inbox = docs_root / "Inbox"

    # has a row → counts
    ready = inbox / "ready.pdf"
    ready.write_text("x", encoding="utf-8")
    ready_id = upsert(Doc(
        path=relative_to_root(ready),
        hash=hash_file(ready),
        size=ready.stat().st_size,
        original=ready.name,
        category="Personal",
        ocr=OcrInfo(decision="never"),
    ))
    enqueue(ready_id)

    # no row → doesn't count
    pending = inbox / "pending.pdf"
    pending.write_text("x", encoding="utf-8")

    # nested folder with a row → counts
    nested = inbox / "sub"
    nested.mkdir(parents=True)
    deep = nested / "deep.pdf"
    deep.write_text("x", encoding="utf-8")
    deep_id = upsert(Doc(
        path=relative_to_root(deep),
        hash=hash_file(deep),
        size=deep.stat().st_size,
        original=deep.name,
        category="Personal",
        ocr=OcrInfo(decision="never"),
    ))
    enqueue(deep_id)

    assert _count_ready_for_triage() == 2


def test_count_ready_for_triage_empty(docs_root):
    from dragndoc.toaster import _count_ready_for_triage
    assert _count_ready_for_triage() == 0


def test_cursor_round_trip(tmp_path):
    cpath = tmp_path / "toaster.cursor"
    Cursor(last_id=42).save(cpath)
    loaded = Cursor.load(cpath)
    assert loaded.last_id == 42


def test_cursor_load_missing_returns_default(tmp_path):
    loaded = Cursor.load(tmp_path / "nope")
    assert loaded.last_id == 0


def test_cursor_load_corrupt_returns_default(tmp_path):
    cpath = tmp_path / "toaster.cursor"
    cpath.write_text("not an int", encoding="utf-8")
    loaded = Cursor.load(cpath)
    assert loaded.last_id == 0


def test_latest_id_reflects_appends(docs_root):
    assert events.latest_id() == 0
    events.append("processed", file="a.pdf")
    assert events.latest_id() > 0


def test_fetch_since_filters_by_id(docs_root):
    events.append("processed", file="a.pdf")
    events.append("processed", file="b.pdf")
    events.append("processed", file="c.pdf")
    rows = events.fetch_since(0)
    assert len(rows) == 3
    second_id = rows[1]["id"]
    rows_after = events.fetch_since(second_id)
    assert len(rows_after) == 1
    assert rows_after[0]["payload"]["file"] == "c.pdf"
