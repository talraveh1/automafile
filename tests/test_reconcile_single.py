"""Tests for single-file scanner reconciliation."""

from __future__ import annotations

import shutil
from pathlib import Path

from dragndoc.meta_store import Doc, OcrInfo, get_by_path, relative_to_root, upsert
from dragndoc.metadata.hashing import hash_file
from dragndoc.scanner import ContentChanged, NewFile, NoChange, Renamed, reconcile_single


def _seed_with_row(file_path: Path, payload: bytes) -> None:
    file_path.write_bytes(payload)
    upsert(Doc(
        path=relative_to_root(file_path),
        hash=hash_file(file_path),
        size=file_path.stat().st_size,
        modified="2026-01-01T00:00:00Z",
        original=file_path.name,
        category="Personal",
        summary="hello",
        ocr=OcrInfo(decision="never"),
    ))


def test_reconcile_single_no_change(docs_root):
    path = docs_root / "Inbox" / "doc.txt"
    _seed_with_row(path, b"hello")
    assert isinstance(reconcile_single(path), NoChange)


def test_reconcile_single_content_changed(docs_root):
    path = docs_root / "Inbox" / "doc.txt"
    _seed_with_row(path, b"hello")
    path.write_bytes(b"changed")
    outcome = reconcile_single(path)
    assert isinstance(outcome, ContentChanged)
    assert outcome.row.path == "Inbox/doc.txt"
    assert outcome.file_hash == hash_file(path)


def test_reconcile_single_renamed(docs_root):
    src = docs_root / "Inbox" / "doc.txt"
    _seed_with_row(src, b"hello")
    moved = docs_root / "Personal" / "doc.txt"
    moved.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(moved))
    outcome = reconcile_single(moved)
    assert isinstance(outcome, Renamed)
    assert get_by_path("Inbox/doc.txt") is None
    assert get_by_path("Personal/doc.txt") is not None


def test_reconcile_single_new_file(docs_root):
    path = docs_root / "Inbox" / "new.txt"
    path.write_bytes(b"new")
    outcome = reconcile_single(path)
    assert isinstance(outcome, NewFile)
    assert outcome.file_hash == hash_file(path)
