"""Tests for the SQLite metadata layer (db.py + meta_store.py)."""

from __future__ import annotations

from pathlib import Path

from dragndoc.db import (
    bootstrap_schema,
    connect,
    from_semilist,
    semilist_contains,
    to_semilist,
    transaction,
)
from dragndoc.meta_store import (
    Doc,
    OcrInfo,
    delete_by_path,
    doc_from_markdown,
    get_by_file,
    get_by_hash,
    get_by_path,
    relative_to_root,
    to_markdown,
    upsert,
)


def test_semilist_round_trip():
    assert to_semilist([]) == ""
    assert to_semilist(["a"]) == ";a;"
    # sort + dedup on write
    assert to_semilist(["b", "a", "a"]) == ";a;b;"
    assert from_semilist("") == []
    assert from_semilist(";a;b;") == ["a", "b"]


def test_semilist_strips_separators_from_values():
    assert to_semilist(["a;b", "c"]) == ";ab;c;"


def test_semilist_contains_uses_wrapped_match():
    s = to_semilist(["tax-2025", "business"])
    assert semilist_contains(s, "tax-2025")
    assert semilist_contains(s, "business")
    # boundary: substring of a value should not match
    assert not semilist_contains(s, "tax")
    assert not semilist_contains(s, "ness")


def test_bootstrap_creates_tables(docs_root):
    from dragndoc.config import get_settings

    settings = get_settings()
    bootstrap_schema(settings.db_path)
    with connect(readonly=True) as conn:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "docs" in names
    assert "ocr" in names
    assert "events" in names


def test_upsert_and_get_by_path(docs_root):
    p = docs_root / "Inbox" / "note.txt"
    p.write_text("hello", encoding="utf-8")
    doc = Doc(
        path=relative_to_root(p),
        hash="sha256:abc",
        size=5,
        original=p.name,
        category="Personal",
        tags=["a", "b"],
        title="t",
        summary="s",
        ocr=OcrInfo(decision="never"),
    )
    upsert(doc)
    fetched = get_by_path(doc.path)
    assert fetched is not None
    assert fetched.title == "t"
    assert fetched.tags == ["a", "b"]
    assert fetched.category == "Personal"


def test_get_by_hash_returns_all_matches(docs_root):
    p1 = docs_root / "Inbox" / "a.txt"
    p2 = docs_root / "Inbox" / "b.txt"
    p1.write_text("x", encoding="utf-8")
    p2.write_text("x", encoding="utf-8")
    upsert(Doc(path=relative_to_root(p1), hash="sha256:dup", size=1, original=p1.name))
    upsert(Doc(path=relative_to_root(p2), hash="sha256:dup", size=1, original=p2.name))
    docs = get_by_hash("sha256:dup")
    paths = sorted(d.path for d in docs)
    assert paths == ["Inbox/a.txt", "Inbox/b.txt"]


def test_upsert_updates_existing_row(docs_root):
    p = docs_root / "Inbox" / "note.txt"
    p.write_text("hello", encoding="utf-8")
    rel = relative_to_root(p)
    upsert(Doc(path=rel, hash="sha256:1", size=5, original=p.name, category="Unknown"))
    upsert(Doc(path=rel, hash="sha256:1", size=5, original=p.name, category="Personal"))
    fetched = get_by_path(rel)
    assert fetched is not None
    assert fetched.category == "Personal"


def test_delete_by_path(docs_root):
    p = docs_root / "Inbox" / "note.txt"
    p.write_text("hello", encoding="utf-8")
    rel = relative_to_root(p)
    upsert(Doc(path=rel, hash="sha256:1", size=5, original=p.name))
    assert get_by_path(rel) is not None
    assert delete_by_path(rel) is True
    assert get_by_path(rel) is None


def test_ocr_row_round_trip(docs_root):
    p = docs_root / "Inbox" / "scan.pdf"
    p.write_bytes(b"%PDF-1.4\n")
    upsert(Doc(
        path=relative_to_root(p),
        hash="sha256:pdf",
        size=p.stat().st_size,
        original=p.name,
        ocr=OcrInfo(
            decision="ocr_full",
            done="2026-05-04T10:00:00Z",
            engine="tesseract",
            engine_ver="5.3.0",
            langs=["heb", "eng"],
        ),
    ))
    fetched = get_by_file(p)
    assert fetched is not None
    assert fetched.ocr.decision == "ocr_full"
    assert fetched.ocr.engine == "tesseract"
    assert fetched.ocr.langs == ["eng", "heb"]  # sorted on write


def test_fts_match_finds_by_summary(docs_root):
    p = docs_root / "Inbox" / "note.txt"
    p.write_text("hi", encoding="utf-8")
    upsert(Doc(
        path=relative_to_root(p),
        hash="sha256:1",
        size=2,
        original=p.name,
        title="Quarterly invoice",
        summary="Total: $1,234 for Q3 services rendered.",
        tags=["tax-2025", "invoice"],
    ))
    with connect(readonly=True) as conn:
        rows = conn.execute(
            "SELECT d.path FROM docs d JOIN docs_fts f ON d.id = f.rowid "
            "WHERE docs_fts MATCH ? ORDER BY bm25(docs_fts)",
            ("invoice",),
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["path"].endswith("note.txt")


def test_fts_tag_membership_via_match(docs_root):
    p = docs_root / "Inbox" / "note.txt"
    p.write_text("hi", encoding="utf-8")
    upsert(Doc(
        path=relative_to_root(p),
        hash="sha256:1",
        size=2,
        original=p.name,
        tags=["tax-2025", "business"],
    ))
    with connect(readonly=True) as conn:
        rows = conn.execute(
            "SELECT d.path FROM docs d JOIN docs_fts f ON d.id = f.rowid "
            "WHERE docs_fts MATCH ?",
            # tags containing punctuation (e.g. `tax-2025`) must be phrase-quoted
            ('tags:"tax-2025" AND tags:business',),
        ).fetchall()
    assert len(rows) == 1


def test_markdown_round_trip(docs_root):
    p = docs_root / "Inbox" / "note.txt"
    p.write_text("hello", encoding="utf-8")
    original = Doc(
        path=relative_to_root(p),
        hash="sha256:1",
        size=5,
        original=p.name,
        category="Finance/Receipts",
        parties=["ACME"],
        tags=["tax-2025"],
        langs=["eng"],
        title="Q3 invoice",
        summary="The summary",
        notes="Some notes",
        confidence="high",
    )
    md = to_markdown(original)
    parsed = doc_from_markdown(md, base=original)
    assert parsed.category == "Finance/Receipts"
    assert parsed.parties == ["ACME"]
    assert parsed.tags == ["tax-2025"]
    assert parsed.title == "Q3 invoice"
    assert parsed.summary == "The summary"
    assert parsed.notes == "Some notes"
    assert parsed.confidence == "high"
