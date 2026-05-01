"""Tests for the OCR decision matrix (no Tesseract calls)."""

from __future__ import annotations

from pathlib import Path

from automafile.ocr import OcrDecision, pdf_ocr_decision
from automafile.config import get_settings


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
