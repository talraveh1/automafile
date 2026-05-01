"""End-to-end pipeline tests with the LLM call patched out."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from automafile.llm import EnrichmentResult
from automafile.pipeline import process_file


_FAKE = EnrichmentResult(
    title="Fake title",
    summary="Fake summary written by the test suite.",
    tags=["a", "b"],
    category="Personal",
    confidence="high",
    needs_review=False,
    language="en",
    tier="strict",
)


def test_process_text_file_writes_sidecar(docs_root):
    p = docs_root / "Inbox" / "note.txt"
    p.write_text("Hebrew test שלום", encoding="utf-8")
    with patch("automafile.pipeline.enrich", return_value=_FAKE):
        result = process_file(p)
    assert result.error is None
    assert result.metadata_target == "sidecar"
    assert result.sidecar_path is not None
    assert result.sidecar_path.exists()
    assert result.category == "Personal"


def test_process_dry_run_does_not_write(docs_root):
    p = docs_root / "Inbox" / "note.txt"
    p.write_text("hi", encoding="utf-8")
    with patch("automafile.pipeline.enrich", return_value=_FAKE):
        result = process_file(p, dry_run=True)
    assert result.metadata_target == "dry_run"
    assert result.sidecar_path is None
    from automafile.metadata import sidecar as sc
    assert not sc.sidecar_path_for(p).exists()


def test_process_pdf_writes_native_metadata_and_sidecar(docs_root):
    import pikepdf
    p = docs_root / "Inbox" / "doc.pdf"
    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(612, 792))
    pdf.save(p)
    with patch("automafile.pipeline.enrich", return_value=_FAKE), \
         patch("automafile.pipeline._maybe_run_ocr") as ocr:
        # patch OCR so we don't actually run Tesseract
        from automafile.metadata.schema import OcrBlock
        from automafile.extractors.base import ExtractedDoc
        def passthrough(doc, decision):
            doc.text = "fake text"
            return doc, OcrBlock(decision="ocr_full")
        ocr.side_effect = passthrough
        result = process_file(p)
    assert result.error is None
    assert result.metadata_target in {"native", "native+sidecar"}
    with pikepdf.open(p) as pdf2:
        with pdf2.open_metadata() as xmp:
            assert "Fake title" in xmp.get("dc:title", "")
