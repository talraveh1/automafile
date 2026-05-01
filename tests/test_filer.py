"""Tests for the file-mover."""

from __future__ import annotations

from pathlib import Path

from automafile.filer import FilingProposal, apply_filing, propose_filing, smart_filename
from automafile.metadata import sidecar
from automafile.metadata.hashing import hash_file
from automafile.metadata.schema import MetadataDoc, OcrBlock


def _seed(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    meta = MetadataDoc(
        content_hash=hash_file(path),
        file_size=path.stat().st_size,
        filename_at_creation=path.name,
        relative_path=str(path.name),
        category="Personal",
        title="Notes",
        date="2026-04-01",
        correspondent="Alice",
        ocr=OcrBlock(decision="never"),
    )
    sidecar.write(path, meta, summary_body="A short summary.")


def test_smart_filename_uses_date_correspondent_topic():
    name = smart_filename(
        {"date": "2026-04-01", "correspondent": "Alice", "title": "Notes", "extension": "txt"},
        "txt",
    )
    assert name == "2026-04-01 - Alice - Notes.txt"


def test_propose_filing_pulls_extension(docs_root):
    p = docs_root / "Inbox" / "note.txt"
    _seed(p, "hello")
    proposal = propose_filing({"category": "Personal", "extension": "txt"})
    assert proposal.category == "Personal"
    assert proposal.smart_name.endswith(".txt")


def test_apply_filing_moves_file_and_sidecar(docs_root):
    p = docs_root / "Inbox" / "note.txt"
    _seed(p, "hello world")
    proposal = FilingProposal(
        category="Personal",
        subcategory=None,
        smart_name="moved.txt",
    )
    target = apply_filing(p, proposal)
    assert target.exists()
    assert not p.exists()
    new_sidecar = sidecar.sidecar_path_for(target)
    assert new_sidecar.exists()
    doc, summary, _ = sidecar.read(target)
    assert doc is not None
    assert doc.filed_at is not None
    assert "moved.txt" in doc.relative_path
