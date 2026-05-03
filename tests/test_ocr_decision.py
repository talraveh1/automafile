"""Tests for the OCR decision matrix (no Tesseract calls)."""

from __future__ import annotations

from pathlib import Path

from dragndoc.ocr import OcrDecision, pdf_ocr_decision
from dragndoc.config import get_settings


def _make_pdf(path: Path, page_texts: list[str]) -> None:
    from pypdf import PdfWriter
    try:
        from reportlab.pdfgen import canvas
    except ImportError:
        # fallback: write a simple PDF using pikepdf with literal text
        import pikepdf
        pdf = pikepdf.new()
        for t in page_texts:
            page = pdf.add_blank_page(page_size=(612, 792))
        pdf.save(path)
        return
    from reportlab.lib.pagesizes import letter
    c = canvas.Canvas(str(path), pagesize=letter)
    for t in page_texts:
        if t:
            c.drawString(72, 720, t)
        c.showPage()
    c.save()


def _write_text_pdf(path: Path, text: str, pages: int = 1) -> None:
    """Build a tiny PDF whose text layer is exactly ``text`` repeated per page."""
    import pikepdf
    from pikepdf import Pdf, Page, PdfImage
    # use pikepdf to construct a minimal PDF with embedded text
    pdf = Pdf.new()
    for _ in range(pages):
        page_obj = pdf.add_blank_page(page_size=(612, 792))
        # embed a content stream that draws the text using a default font
        font = pdf.make_indirect(pikepdf.Dictionary({
            "/Type": pikepdf.Name("/Font"),
            "/Subtype": pikepdf.Name("/Type1"),
            "/BaseFont": pikepdf.Name("/Helvetica"),
        }))
        resources = pikepdf.Dictionary({"/Font": pikepdf.Dictionary({"/F1": font})})
        # stream content: BT /F1 12 Tf 72 720 Td (text) Tj ET
        escaped = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("ascii", errors="replace")
        page_obj.contents_add(pikepdf.Stream(pdf, stream))
        page_obj.Resources = resources
    pdf.save(path)


def test_pdf_with_rich_text_layer_no_ocr(tmp_path):
    pdf_path = tmp_path / "rich.pdf"
    rich = "lorem ipsum dolor sit amet " * 30
    _write_text_pdf(pdf_path, rich, pages=2)
    decision = pdf_ocr_decision(pdf_path)
    assert decision.action == "no_ocr", f"unexpected: {decision}"


def test_pdf_with_no_text_layer_full_ocr(tmp_path):
    pdf_path = tmp_path / "scan.pdf"
    import pikepdf
    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(612, 792))
    pdf.save(pdf_path)
    decision = pdf_ocr_decision(pdf_path)
    assert decision.action == "ocr_full"
    assert "no_text_layer" in decision.reason


def test_decision_with_supplied_per_page_chars_skips_pdf_parse(tmp_path):
    """When per_page_chars is supplied, the decision skips its own pypdf parse."""
    fake_path = tmp_path / "does-not-exist.pdf"
    # this would raise if pdf_ocr_decision tried to open the file
    decision = pdf_ocr_decision(fake_path, per_page_chars=[1000, 1000, 1000])
    assert decision.action == "no_ocr"


def test_sparse_pages_branch():
    """Most pages have text, a couple don't — flag those for partial OCR."""
    from pathlib import Path
    chars = [800, 800, 0, 800, 800, 800, 800, 800, 800, 800]  # 1 sparse out of 10
    decision = pdf_ocr_decision(Path("/dev/null"), per_page_chars=chars)
    assert decision.action == "ocr_pages"
    assert decision.pages == [2]


def test_majority_sparse_falls_back_to_full():
    from pathlib import Path
    chars = [0, 0, 0, 0, 800, 800]  # 4 sparse out of 6 — exceeds default 0.3 ratio
    decision = pdf_ocr_decision(Path("/dev/null"), per_page_chars=chars)
    assert decision.action == "ocr_full"
    assert decision.reason == "majority_sparse"


def test_skip_encrypted(tmp_path):
    import pikepdf
    pdf_path = tmp_path / "secret.pdf"
    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(612, 792))
    pdf.save(pdf_path, encryption=pikepdf.Encryption(owner="o", user="u"))
    decision = pdf_ocr_decision(pdf_path)
    assert decision.action == "skip_encrypted"


def test_coalesce_page_ranges():
    from dragndoc.ocr import _coalesce_page_ranges
    # zero-based input → one-based (first, last) pairs
    assert _coalesce_page_ranges([]) == []
    assert _coalesce_page_ranges([0]) == [(1, 1)]
    assert _coalesce_page_ranges([0, 1, 2]) == [(1, 3)]
    assert _coalesce_page_ranges([0, 2, 3, 5]) == [(1, 1), (3, 4), (6, 6)]
    # unsorted + duplicates
    assert _coalesce_page_ranges([5, 0, 2, 3, 0]) == [(1, 1), (3, 4), (6, 6)]
