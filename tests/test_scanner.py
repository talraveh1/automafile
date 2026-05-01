"""Smoke tests for the tree scanner."""

from __future__ import annotations

from pathlib import Path

from automafile.metadata import sidecar
from automafile.metadata.hashing import hash_file
from automafile.metadata.schema import MetadataDoc, OcrBlock
from automafile.scanner import run_scan, write_worklist


def test_run_scan_on_empty_tree(docs_root):
    wl = run_scan()
    assert wl.files_seen == 0
    assert wl.files_needing_metadata == []


def test_run_scan_flags_unmetadataed_text_file(docs_root):
    p = docs_root / "Inbox" / "note.txt"
    p.write_text("hello world", encoding="utf-8")
    wl = run_scan()
    assert wl.files_seen == 1
    assert any(f["relative_path"].endswith("note.txt") for f in wl.files_needing_metadata)


def test_run_scan_writes_worklist_file(docs_root):
    p = docs_root / "Inbox" / "note.txt"
    p.write_text("hello", encoding="utf-8")
    wl = run_scan()
    out = write_worklist(wl)
    assert out.exists()
    assert out.read_text(encoding="utf-8").startswith("{")


def test_partial_metadata_detected(docs_root):
    p = docs_root / "Inbox" / "doc.txt"
    p.write_text("hi", encoding="utf-8")
    meta = MetadataDoc(
        content_hash=hash_file(p),
        file_size=p.stat().st_size,
        filename_at_creation=p.name,
        relative_path="Inbox/doc.txt",
        category="Unknown",
        ocr=OcrBlock(decision="never"),
    )
    sidecar.write(p, meta, summary_body="")
    wl = run_scan()
    rels = [f["relative_path"] for f in wl.files_with_partial_metadata]
    assert any("doc.txt" in r for r in rels)
