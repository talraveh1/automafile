"""Sidecar reader / writer tests."""

from __future__ import annotations

from pathlib import Path

from automafile.metadata import sidecar
from automafile.metadata.schema import MetadataDoc, OcrBlock


def _build_meta(file_path: Path) -> MetadataDoc:
    from automafile.metadata.hashing import hash_file
    return MetadataDoc(
        content_hash=hash_file(file_path),
        file_size=file_path.stat().st_size,
        filename_at_creation=file_path.name,
        relative_path=str(file_path.name),
        language="en",
        tags=["alpha", "beta"],
        category="Financial",
        confidence="high",
        ocr=OcrBlock(decision="never"),
    )


def test_write_and_read_roundtrip(docs_root):
    f = docs_root / "Inbox" / "note.txt"
    f.write_text("hello world", encoding="utf-8")
    meta = _build_meta(f)
    spath = sidecar.write(f, meta, summary_body="A short summary.", notes_body="some notes")
    assert spath.exists()
    assert spath.parent.name == ".meta"
    assert spath.name == "note.txt.md"

    loaded, summary, notes = sidecar.read(f)
    assert loaded is not None
    assert loaded.tags == ["alpha", "beta"]
    assert loaded.category == "Financial"
    assert summary == "A short summary."
    assert notes == "some notes"


def test_sidecar_path_layout(docs_root):
    f = docs_root / "Inbox" / "report.pdf"
    f.write_bytes(b"x")
    spath = sidecar.sidecar_path_for(f)
    assert spath.parent == docs_root / "Inbox" / ".meta"
    assert spath.name == "report.pdf.md"


def test_existing_body_preserved_when_summary_empty(docs_root):
    f = docs_root / "Inbox" / "note.txt"
    f.write_text("hi", encoding="utf-8")
    meta = _build_meta(f)
    sidecar.write(f, meta, summary_body="initial", notes_body="initial-notes")
    sidecar.write(f, meta, summary_body="", notes_body=None)
    loaded, summary, notes = sidecar.read(f)
    assert summary == "initial"
    assert notes == "initial-notes"
