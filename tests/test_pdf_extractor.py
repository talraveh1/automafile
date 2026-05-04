"""Tests for sectioned PDF extraction."""

from __future__ import annotations

from pathlib import Path

import pytest


def _write_text_pdf(path: Path, page_texts: list[str]) -> None:
    pikepdf = pytest.importorskip("pikepdf")
    pdf = pikepdf.Pdf.new()
    for text in page_texts:
        page_obj = pdf.add_blank_page(page_size=(612, 792))
        font = pdf.make_indirect(pikepdf.Dictionary({
            "/Type": pikepdf.Name("/Font"),
            "/Subtype": pikepdf.Name("/Type1"),
            "/BaseFont": pikepdf.Name("/Helvetica"),
        }))
        resources = pikepdf.Dictionary({"/Font": pikepdf.Dictionary({"/F1": font})})
        escaped = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("ascii", errors="replace")
        page_obj.contents_add(pikepdf.Stream(pdf, stream))
        page_obj.Resources = resources
    pdf.save(path)


def test_pdf_caps_hundred_pages_to_five_sections(tmp_path, monkeypatch):
    from dragndoc.extractors import pdf as pdf_ext

    monkeypatch.setattr(pdf_ext, "tesseract_available", lambda: False)
    path = tmp_path / "many.pdf"
    _write_text_pdf(path, [f"page {i} " + ("text " * 80) for i in range(100)])
    doc = pdf_ext.extract(path)
    assert len(doc.sections) == 5
    assert doc.total_sections == 100
    assert doc.sections[0].label == "Page 1"
    assert doc.sections[-1].label == "Page 5"


def test_pdf_keeps_two_page_document(tmp_path, monkeypatch):
    from dragndoc.extractors import pdf as pdf_ext

    monkeypatch.setattr(pdf_ext, "tesseract_available", lambda: False)
    path = tmp_path / "two.pdf"
    _write_text_pdf(path, ["first page", "second page"])
    doc = pdf_ext.extract(path)
    assert len(doc.sections) == 2
    assert doc.total_sections == 2
    assert [section.label for section in doc.sections] == ["Page 1", "Page 2"]


def test_pdf_thin_pages_expand_to_max(tmp_path, monkeypatch):
    from dragndoc.extractors import pdf as pdf_ext

    monkeypatch.setattr(pdf_ext, "tesseract_available", lambda: False)
    path = tmp_path / "thin.pdf"
    _write_text_pdf(path, [f"thin {i}" for i in range(12)])
    doc = pdf_ext.extract(path)
    assert len(doc.sections) == 5
    assert doc.total_sections == 12


def test_pdf_fat_first_page_is_trimmed_but_min_pages_are_kept(tmp_path, monkeypatch):
    from dragndoc.config import reset_settings
    from dragndoc.extractors import pdf as pdf_ext

    monkeypatch.setattr(pdf_ext, "tesseract_available", lambda: False)
    monkeypatch.setenv("OCR_MAX_TOTAL_CHARS", "1500")
    reset_settings()
    path = tmp_path / "fat.pdf"
    _write_text_pdf(path, ["alpha " * 2000, "second page", "third page", "fourth page"])
    doc = pdf_ext.extract(path)
    assert len(doc.sections) == 3
    assert len(doc.sections[0].text) <= 1500
    assert doc.sections[0].text.endswith("alpha")
    reset_settings()
