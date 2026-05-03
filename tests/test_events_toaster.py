"""Events journal + toaster cursor/compaction tests."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from automafile import events
from automafile import toaster
from automafile.toaster import Cursor, _consume, _format_toast, _maybe_compact


def _read_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_append_writes_jsonl_with_ts_and_kind():
    events.append("processed", file="foo.pdf", category="Receipts")
    records = _read_lines(events.events_path())
    assert len(records) == 1
    assert records[0]["kind"] == "processed"
    assert records[0]["file"] == "foo.pdf"
    assert records[0]["category"] == "Receipts"
    assert records[0]["ts"].endswith("Z")


def test_append_creates_storage_dir():
    path = events.events_path()
    assert not path.parent.exists()
    events.append("processed", file="foo.pdf")
    assert path.exists()


def test_append_multiple_events_preserves_order():
    for i in range(5):
        events.append("processed", file=f"f{i}.pdf")
    records = _read_lines(events.events_path())
    assert [r["file"] for r in records] == [f"f{i}.pdf" for i in range(5)]


def test_format_toast_processed():
    title, body = _format_toast({"kind": "processed", "file": "f.pdf", "category": "Receipts", "target": "Receipts/2026-01 X.pdf"})
    assert title == "Automafile"
    assert "f.pdf" in body
    assert "Receipts" in body
    assert "Receipts/2026-01 X.pdf" in body


def test_format_toast_quarantined():
    title, body = _format_toast({"kind": "quarantined", "file": "Inbox/x.pdf.md", "reason": "yaml_error: foo"})
    assert title == "Sidecar quarantined"
    assert "x.pdf.md" in body
    assert "yaml_error" in body


def test_format_toast_unknown_kind_falls_through():
    title, body = _format_toast({"kind": "weird", "extra": "data"})
    assert title == "Automafile"
    assert "weird" in body


def test_consume_advances_cursor_and_fires_toast(docs_root):
    events.append("processed", file="a.pdf", category="X")
    events.append("processed", file="b.pdf", category="Y")

    notifier = MagicMock()
    cursor = _consume(events.events_path(), Cursor(), notifier)

    assert notifier.notify.call_count == 2
    assert cursor.offset > 0
    assert cursor.size_seen == cursor.offset


def test_consume_resumes_from_saved_cursor(docs_root):
    events.append("processed", file="a.pdf")
    notifier = MagicMock()
    cursor = _consume(events.events_path(), Cursor(), notifier)
    assert notifier.notify.call_count == 1

    events.append("processed", file="b.pdf")
    notifier2 = MagicMock()
    cursor = _consume(events.events_path(), cursor, notifier2)
    assert notifier2.notify.call_count == 1
    notifier2.notify.assert_called_once()
    assert "b.pdf" in notifier2.notify.call_args[0][1]


def test_consume_handles_partial_trailing_line(docs_root):
    """A half-written final line is left for the next tick, not dropped."""
    path = events.events_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'{"ts":"t","kind":"processed","file":"a"}\n{"ts":"t","kind":"proce')

    notifier = MagicMock()
    cursor = _consume(path, Cursor(), notifier)
    assert notifier.notify.call_count == 1
    # cursor should sit at the start of the partial line
    assert cursor.offset == len(b'{"ts":"t","kind":"processed","file":"a"}\n')


def test_consume_skips_malformed_lines(docs_root):
    path = events.events_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'not-json\n{"ts":"t","kind":"processed","file":"a"}\n')

    notifier = MagicMock()
    _consume(path, Cursor(), notifier)
    assert notifier.notify.call_count == 1


def test_consume_resets_cursor_when_file_shrinks(docs_root):
    """Detection of external compaction: file size dropped below what we last saw."""
    for i in range(10):
        events.append("processed", file=f"file-{i}.pdf")
    path = events.events_path()
    cursor = _consume(path, Cursor(), MagicMock())
    pre_truncate_offset = cursor.offset

    # simulate external compaction
    path.write_text("", encoding="utf-8")
    events.append("processed", file="b.pdf")

    notifier = MagicMock()
    cursor = _consume(path, cursor, notifier)
    assert notifier.notify.call_count == 1
    assert cursor.offset < pre_truncate_offset


def test_compact_truncates_when_over_threshold_and_caught_up(docs_root, monkeypatch):
    monkeypatch.setattr(toaster, "COMPACT_THRESHOLD_BYTES", 100)

    for i in range(20):
        events.append("processed", file=f"file-{i}.pdf", category="X")

    path = events.events_path()
    assert path.stat().st_size > 100

    cursor = _consume(path, Cursor(), MagicMock())
    assert cursor.offset == path.stat().st_size

    cursor = _maybe_compact(path, cursor)
    assert path.stat().st_size == 0
    assert cursor.offset == 0
    assert cursor.size_seen == 0


def test_compact_skipped_when_cursor_behind(docs_root, monkeypatch):
    monkeypatch.setattr(toaster, "COMPACT_THRESHOLD_BYTES", 100)

    for i in range(20):
        events.append("processed", file=f"file-{i}.pdf")

    path = events.events_path()
    pre_size = path.stat().st_size
    # cursor is at 0 — behind
    cursor = _maybe_compact(path, Cursor())
    assert path.stat().st_size == pre_size
    assert cursor.offset == 0


def test_cursor_round_trip(tmp_path):
    cpath = tmp_path / "toaster.cursor"
    Cursor(offset=42, size_seen=99).save(cpath)
    loaded = Cursor.load(cpath)
    assert loaded.offset == 42
    assert loaded.size_seen == 99


def test_cursor_load_missing_returns_default(tmp_path):
    loaded = Cursor.load(tmp_path / "nope")
    assert loaded.offset == 0
    assert loaded.size_seen == 0


def test_cursor_load_corrupt_returns_default(tmp_path):
    cpath = tmp_path / "toaster.cursor"
    cpath.write_text("not json", encoding="utf-8")
    loaded = Cursor.load(cpath)
    assert loaded.offset == 0
