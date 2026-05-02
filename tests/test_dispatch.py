"""Dispatcher and per-format extractor smoke tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from automafile.dispatch import EXT_MAP, get_extractor
from automafile.extractors import (
    docx as docx_ext,
    image as image_ext,
    pdf as pdf_ext,
    text as text_ext,
)


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


def test_unknown_extension_falls_back(tmp_path):
    p = tmp_path / "foo.weirdext"
    p.write_text("hello")
    extractor = get_extractor(p)
    # accept either unknown or magic-detected; both are valid
    assert extractor is not None
    doc = extractor.extract(p)
    assert doc.path == p


def test_ext_map_covers_expected_formats():
    for ext in (".pdf", ".docx", ".xlsx", ".pptx", ".jpg", ".png", ".html", ".epub", ".txt"):
        assert ext in EXT_MAP
