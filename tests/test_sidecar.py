"""Sidecar reader / writer tests."""

from __future__ import annotations

from pathlib import Path

from dragndoc.metadata import sidecar
from dragndoc.metadata.schema import MetadataDoc, OcrBlock


def _build_meta(file_path: Path) -> MetadataDoc:
    from dragndoc.metadata.hashing import hash_file
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


def test_corrupt_sidecar_is_quarantined_not_clobbered(docs_root):
    """A sidecar with broken YAML is renamed aside, NOT silently overwritten."""
    f = docs_root / "Inbox" / "note.txt"
    f.write_text("hi", encoding="utf-8")
    spath = sidecar.sidecar_path_for(f)
    spath.parent.mkdir(parents=True, exist_ok=True)
    spath.write_text(
        "---\nthis is: : : not valid yaml [[[\n---\n# Summary\n\nuser-edited body\n",
        encoding="utf-8",
    )
    doc, summary, notes = sidecar.read(f)
    assert doc is None
    assert summary == ""
    assert notes == ""
    # original is gone, quarantined backup is present
    assert not spath.exists()
    backups = list(spath.parent.glob(f"{spath.name}.broken-*"))
    assert len(backups) == 1
    backup = backups[0]
    # body was preserved in the quarantined file (i.e., we did NOT clobber)
    assert "user-edited body" in backup.read_text(encoding="utf-8")


def test_missing_sidecar_returns_none_without_quarantine(docs_root):
    f = docs_root / "Inbox" / "absent.txt"
    f.write_text("hi", encoding="utf-8")
    doc, summary, notes = sidecar.read(f)
    assert doc is None
    # no .broken-* artifacts created for genuinely-missing sidecars
    meta_dir = sidecar.sidecar_path_for(f).parent
    if meta_dir.exists():
        assert not list(meta_dir.glob("*.broken-*"))
