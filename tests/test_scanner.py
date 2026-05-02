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


def test_write_worklist_skips_when_everything_already_queued(docs_root):
    p = docs_root / "Inbox" / "note.txt"
    p.write_text("hello", encoding="utf-8")
    first = write_worklist(run_scan())
    assert first is not None and first.exists()

    second = write_worklist(run_scan())
    assert second is None, "second scan with no new files should not write a worklist"


def test_write_worklist_writes_only_new_entries(docs_root):
    a = docs_root / "Inbox" / "a.txt"
    a.write_text("a", encoding="utf-8")
    first = write_worklist(run_scan())
    assert first is not None

    b = docs_root / "Inbox" / "b.txt"
    b.write_text("b", encoding="utf-8")
    second = write_worklist(run_scan())
    assert second is not None and second != first
    import json as _json
    data = _json.loads(second.read_text(encoding="utf-8"))
    rels = [e["relative_path"] for e in data["files_needing_metadata"]]
    assert any(r.endswith("b.txt") for r in rels)
    assert not any(r.endswith("a.txt") for r in rels), "a.txt was already queued in the first worklist"


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


def test_ocr_review_candidates_when_engine_drifts(docs_root, monkeypatch):
    """If a sidecar records a different engine_version/langs than current, surface for review."""
    p = docs_root / "Inbox" / "scan.pdf"
    # any bytes will do; the scanner only looks at the sidecar's recorded engine
    p.write_bytes(b"%PDF-1.4\n%fake\n")
    meta = MetadataDoc(
        content_hash=hash_file(p),
        file_size=p.stat().st_size,
        filename_at_creation=p.name,
        relative_path="Inbox/scan.pdf",
        category="Personal",
        title="x",
        tags=["a"],
        ocr=OcrBlock(
            decision="ocr_full",
            done_at="2026-01-01T00:00:00Z",
            engine="tesseract",
            engine_version="tesseract 4.1",
            languages="eng",
        ),
    )
    sidecar.write(p, meta, summary_body="something")
    monkeypatch.setattr("automafile.scanner.tesseract_version", lambda: "tesseract 5.5.0")
    monkeypatch.setenv("TESSERACT_LANGS", "heb+eng")
    from automafile.config import reset_settings
    reset_settings()
    wl = run_scan()
    rels = [c["relative_path"] for c in wl.ocr_review_candidates]
    assert any("scan.pdf" in r for r in rels)


def test_quarantined_sidecars_surfaced_in_worklist(docs_root):
    p = docs_root / "Inbox" / "broken.txt"
    p.write_text("hello", encoding="utf-8")
    # write a sidecar manually with malformed YAML so read() will quarantine it
    spath = sidecar.sidecar_path_for(p)
    spath.parent.mkdir(parents=True, exist_ok=True)
    spath.write_text("---\nthis is: : : invalid yaml [[[\n---\nbody\n", encoding="utf-8")
    # trigger quarantine by reading
    sidecar.read(p)
    # the original sidecar is now renamed; scanner should surface it
    wl = run_scan()
    assert wl.quarantined_sidecars
    entry = wl.quarantined_sidecars[0]
    assert "broken.txt" in entry["for_file"]
    assert ".broken-" in entry["quarantine_relative_path"]
