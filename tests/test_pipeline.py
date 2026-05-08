"""End-to-end pipeline tests with the LLM call patched out."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from dragndoc.llm import EnrichmentResult
from dragndoc.meta_store import get_by_file
from dragndoc.meta_store import OcrInfo
from dragndoc.pipeline import digest_file


_FAKE = EnrichmentResult(
    title="Fake title",
    summary="Fake summary written by the test suite.",
    tags=["a", "b"],
    category="Personal",
    confidence="high",
    review=False,
    language="en",
    tier="strict",
)


def test_digest_text_file_writes_row(docs_root):
    p = docs_root / "Inbox" / "note.txt"
    p.write_text("Hebrew test שלום", encoding="utf-8")
    with patch("dragndoc.pipeline.enrich", return_value=_FAKE):
        result = digest_file(p)
    assert result.error is None
    assert result.metadata_target == "db"
    assert result.doc_id is not None
    doc = get_by_file(p)
    assert doc is not None
    assert doc.category == "Personal"
    assert "a" in doc.tags and "b" in doc.tags
    assert doc.title == "Fake title"


def test_digest_dry_run_does_not_write(docs_root):
    p = docs_root / "Inbox" / "note.txt"
    p.write_text("hi", encoding="utf-8")
    with patch("dragndoc.pipeline.enrich", return_value=_FAKE):
        result = digest_file(p, dry_run=True)
    assert result.metadata_target == "dry_run"
    assert result.doc_id is None
    assert get_by_file(p) is None


def test_digest_file_with_hash_skips_internal_hash(docs_root):
    p = docs_root / "Inbox" / "note.txt"
    p.write_text("hi", encoding="utf-8")
    with patch("dragndoc.pipeline.enrich", return_value=_FAKE), \
         patch("dragndoc.pipeline.hash_file") as mocked_hash:
        result = digest_file(p, file_hash="sha256:provided")
    assert result.error is None
    assert mocked_hash.call_count == 0
    doc = get_by_file(p)
    assert doc is not None
    assert doc.hash == "sha256:provided"


def test_digest_skips_blocked_meta_tree(docs_root):
    blocked_dir = docs_root / "Inbox" / "bundle"
    blocked_dir.mkdir(parents=True, exist_ok=True)
    (blocked_dir / ".meta").write_text("marker", encoding="utf-8")

    p = blocked_dir / "note.txt"
    p.write_text("hi", encoding="utf-8")

    with patch("dragndoc.pipeline.enrich", return_value=_FAKE):
        result = digest_file(p)

    assert result.error == "blocked_by_meta_file"
    assert result.metadata_target == "skipped"
    assert get_by_file(p) is None


def test_digest_pdf_writes_row_only(docs_root):
    """PDFs (like every other format) get a metadata row, never native metadata in the file."""
    import pikepdf
    p = docs_root / "Inbox" / "doc.pdf"
    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(612, 792))
    pdf.save(p)
    with patch("dragndoc.pipeline.enrich", return_value=_FAKE), \
         patch("dragndoc.pipeline._maybe_run_ocr") as ocr:
        def passthrough(doc, decision):
            doc.text = "fake text"
            return doc, OcrInfo(decision="ocr_full")
        ocr.side_effect = passthrough
        result = digest_file(p)
    assert result.error is None
    assert result.metadata_target == "db"
    assert result.doc_id is not None
    # the PDF itself must be untouched — no XMP injected by the pipeline
    with pikepdf.open(p) as pdf2:
        with pdf2.open_metadata() as xmp:
            assert "Fake title" not in xmp.get("dc:title", "")
