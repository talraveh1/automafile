"""Tests for the filer (file move + DB row update)."""

from __future__ import annotations

from pathlib import Path

import pytest

from dragndoc.filer import FilingProposal, TargetCollision, apply_filing, propose_filing, smart_filename
from dragndoc.meta_store import (
    Doc,
    OcrInfo,
    get_by_file,
    relative_to_root,
    upsert,
)
from dragndoc.metadata.hashing import hash_file


def _seed(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    upsert(Doc(
        path=relative_to_root(path),
        hash=hash_file(path),
        size=path.stat().st_size,
        original=path.name,
        category="Personal",
        title="Notes",
        date="2026-04-01",
        parties=["Alice"],
        summary="A short summary.",
        ocr=OcrInfo(decision="never"),
    ))


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


def test_apply_filing_moves_file_and_updates_row(docs_root):
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
    doc = get_by_file(target)
    assert doc is not None
    assert doc.path.endswith("moved.txt")
    assert doc.category == "Personal"


def test_apply_filing_idempotent_on_same_hash(docs_root):
    """If target already exists with identical content, the source is removed; no error."""
    p = docs_root / "Inbox" / "note.txt"
    _seed(p, "hello world")
    proposal = FilingProposal(category="Personal", subcategory=None, smart_name="moved.txt")
    apply_filing(p, proposal)
    p2 = docs_root / "Inbox" / "note.txt"
    _seed(p2, "hello world")
    target = apply_filing(p2, proposal)
    assert target.exists()
    assert not p2.exists()


def test_apply_filing_raises_collision_on_different_hash(docs_root):
    p = docs_root / "Inbox" / "note.txt"
    _seed(p, "hello world")
    proposal = FilingProposal(category="Personal", subcategory=None, smart_name="moved.txt")
    apply_filing(p, proposal)
    p2 = docs_root / "Inbox" / "note.txt"
    _seed(p2, "different content here")
    with pytest.raises(TargetCollision):
        apply_filing(p2, proposal)


def test_apply_filing_overwrite_replaces_target(docs_root):
    p = docs_root / "Inbox" / "note.txt"
    _seed(p, "first version")
    proposal = FilingProposal(category="Personal", subcategory=None, smart_name="moved.txt")
    target = apply_filing(p, proposal)
    p2 = docs_root / "Inbox" / "note.txt"
    _seed(p2, "second version")
    apply_filing(p2, proposal, overwrite=True)
    assert target.read_text(encoding="utf-8") == "second version"


def test_subcategory_folds_into_category_path(docs_root):
    p = docs_root / "Inbox" / "note.txt"
    _seed(p, "hello")
    proposal = FilingProposal(category="Personal", subcategory="2026", smart_name="moved.txt")
    target = apply_filing(p, proposal)
    doc = get_by_file(target)
    assert doc is not None
    assert doc.category == "Personal/2026"
