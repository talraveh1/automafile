"""Smoke tests for the tree scanner (DB-backed)."""

from __future__ import annotations

import logging
from pathlib import Path

from dragndoc.meta_store import Doc, OcrInfo, relative_to_root, upsert
from dragndoc.metadata.hashing import hash_file
from dragndoc.scanner import run_scan


def _seed_row(path: Path, **kwargs) -> None:
    """Write a file and a metadata row pointing at it."""
    upsert(Doc(
        path=relative_to_root(path),
        hash=hash_file(path),
        size=path.stat().st_size,
        original=path.name,
        **kwargs,
    ))


def test_run_scan_on_empty_tree(docs_root):
    wl = run_scan()
    assert wl.files_seen == 0
    assert wl.files_needing_metadata == []


def test_run_scan_flags_unrowed_text_file(docs_root):
    p = docs_root / "Inbox" / "note.txt"
    p.write_text("hello world", encoding="utf-8")
    wl = run_scan()
    assert wl.files_seen == 1
    assert any(f["relative_path"].endswith("note.txt") for f in wl.files_needing_metadata)


def test_run_scan_skips_opaque_subtree(docs_root):
    keep = docs_root / "Inbox" / "keep.txt"
    keep.write_text("keep", encoding="utf-8")

    blocked_dir = docs_root / "Inbox" / ".venv"
    blocked_dir.mkdir(parents=True, exist_ok=True)

    skipped = blocked_dir / "nested" / "skip.txt"
    skipped.parent.mkdir(parents=True, exist_ok=True)
    skipped.write_text("skip", encoding="utf-8")

    wl = run_scan()
    rels = [entry["relative_path"] for entry in wl.files_needing_metadata]

    assert wl.files_seen == 1
    assert "Inbox/keep.txt" in rels
    assert not any(".venv/" in rel for rel in rels)


def test_run_scan_subpath_overrides_parent_opacity(docs_root):
    blocked_dir = docs_root / "Inbox" / ".venv"
    blocked_dir.mkdir(parents=True, exist_ok=True)

    nested = blocked_dir / "nested"
    nested.mkdir(parents=True, exist_ok=True)
    target = nested / "note.txt"
    target.write_text("scan me", encoding="utf-8")

    wl = run_scan(subpath=Path("Inbox/.venv/nested"))
    rels = [entry["relative_path"] for entry in wl.files_needing_metadata]

    assert wl.files_seen == 1
    assert rels == ["Inbox/.venv/nested/note.txt"]


def test_run_scan_logs_directories_at_info(docs_root, caplog):
    alpha = docs_root / "Inbox" / "alpha.txt"
    alpha.write_text("a", encoding="utf-8")
    nested = docs_root / "Inbox" / "nested"
    nested.mkdir(parents=True, exist_ok=True)
    beta = nested / "beta.txt"
    beta.write_text("b", encoding="utf-8")

    with caplog.at_level(logging.INFO):
        run_scan()

    messages = [record.getMessage() for record in caplog.records if record.name == "dragndoc.scanner"]
    assert any("scan: entering" in message and str(alpha.parent) in message for message in messages)
    assert any("scan: entering" in message and str(beta.parent) in message for message in messages)


def test_partial_metadata_detected(docs_root):
    p = docs_root / "Inbox" / "doc.txt"
    p.write_text("hi", encoding="utf-8")
    _seed_row(p, category="Unknown", ocr=OcrInfo(decision="never"))
    wl = run_scan()
    rels = [f["relative_path"] for f in wl.files_with_partial_metadata]
    assert any("doc.txt" in r for r in rels)


def test_ocr_review_candidates_when_engine_drifts(docs_root, monkeypatch):
    """If a row records a different engine_ver/langs than current, surface for review."""
    p = docs_root / "Inbox" / "scan.pdf"
    p.write_bytes(b"%PDF-1.4\n%fake\n")
    _seed_row(
        p,
        category="Personal",
        title="x",
        tags=["a"],
        summary="something",
        ocr=OcrInfo(
            decision="ocr_full",
            done="2026-01-01T00:00:00Z",
            engine="tesseract",
            engine_ver="tesseract 4.1",
            langs=["eng"],
        ),
    )
    monkeypatch.setattr("dragndoc.scanner.tesseract_version", lambda: "tesseract 5.5.0")
    monkeypatch.setenv("TESSERACT_LANGS", "heb+eng")
    from dragndoc.config import reset_settings
    reset_settings()
    wl = run_scan()
    rels = [c["relative_path"] for c in wl.ocr_review_candidates]
    assert any("scan.pdf" in r for r in rels)


def test_missing_files_surfaced(docs_root):
    """A row whose file no longer exists shows up as missing."""
    p = docs_root / "Inbox" / "gone.txt"
    p.write_text("temporary", encoding="utf-8")
    _seed_row(p, category="Personal", ocr=OcrInfo(decision="never"))
    p.unlink()
    wl = run_scan()
    rels = [f["relative_path"] for f in wl.missing_files]
    assert any("gone.txt" in r for r in rels)
