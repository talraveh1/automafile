"""Dispatcher and per-format extractor smoke tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from dragndoc.dispatch import EXT_MAP, extract as dispatch_extract, get_extractor
from dragndoc.extractors import (
    docx as docx_ext,
    image as image_ext,
    pdf as pdf_ext,
    text as text_ext,
)
from dragndoc.extractors.base import CorruptDocumentError


def test_text_extension_maps_to_text(tmp_path):
    p = tmp_path / "foo.txt"
    p.write_text("hello")
    assert get_extractor(p) is text_ext


def test_pdf_extension_maps_to_pdf(tmp_path):
    p = tmp_path / "foo.pdf"
    p.write_bytes(b"%PDF-1.4\n")
    assert get_extractor(p) is pdf_ext


def test_image_extensions_map_to_image(tmp_path):
    for ext in (".jpg", ".jpeg", ".png", ".webp", ".heic"):
        p = tmp_path / f"foo{ext}"
        p.write_bytes(b"")
        assert get_extractor(p) is image_ext


def test_docx_extension_maps_to_docx(tmp_path):
    p = tmp_path / "foo.docx"
    p.write_bytes(b"PK\x03\x04")
    assert get_extractor(p) is docx_ext


def test_text_extractor_reads_utf8(tmp_path):
    p = tmp_path / "foo.txt"
    body = "שלום עולם hello"
    p.write_text(body, encoding="utf-8")
    doc = text_ext.extract(p)
    assert doc.text == body
    assert doc.format == "text"


def test_text_extractor_strict_mode_fails_garbled_binary(tmp_path):
    p = tmp_path / "foo.txt"
    p.write_bytes(b"%PDF-1.7\nnot really enough for a pdf")
    with pytest.raises(CorruptDocumentError):
        text_ext.extract(p, strict=True)


def test_unknown_extension_falls_back(tmp_path):
    p = tmp_path / "foo.weirdext"
    p.write_text("hello")
    extractor = get_extractor(p)
    # accept either unknown or magic-detected; both are valid
    assert extractor is not None
    doc = extractor.extract(p)
    assert doc.path == p


def test_dispatch_retries_sniffed_mime_after_bad_text_extension(tmp_path, monkeypatch):
    pikepdf = pytest.importorskip("pikepdf")
    from dragndoc.extractors import pdf as pdf_module
    import dragndoc.dispatch as dispatch

    p = tmp_path / "renamed.txt"
    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(612, 792))
    pdf.save(p)

    monkeypatch.setattr(dispatch, "_sniff_mime", lambda _path: "application/pdf")
    monkeypatch.setattr(pdf_module, "tesseract_available", lambda: False)

    doc = dispatch_extract(p)
    assert doc.format == "pdf"
    assert doc.total_sections == 1


def test_dispatch_uses_sniffed_mime_for_unknown_extension(tmp_path, monkeypatch):
    import dragndoc.dispatch as dispatch

    p = tmp_path / "data.blob"
    p.write_text("plain text despite the extension", encoding="utf-8")
    monkeypatch.setattr(dispatch, "_sniff_mime", lambda _path: "text/plain")

    doc = dispatch_extract(p)
    assert doc.format == "text"
    assert doc.text == "plain text despite the extension"


def test_dispatch_falls_back_to_unknown_when_known_parser_and_sniff_fail(tmp_path, monkeypatch):
    import dragndoc.dispatch as dispatch

    p = tmp_path / "not-a-pdf.pdf"
    p.write_text("plain fallback body", encoding="utf-8")
    monkeypatch.setattr(dispatch, "_sniff_mime", lambda _path: None)

    doc = dispatch_extract(p)
    assert doc.format == "unknown"
    assert doc.text == "plain fallback body"


def test_ext_map_covers_expected_formats():
    for ext in (".pdf", ".docx", ".xlsx", ".pptx", ".jpg", ".png", ".html", ".epub", ".txt"):
        assert ext in EXT_MAP
