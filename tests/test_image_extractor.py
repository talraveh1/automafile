"""Tests for sectioned image extraction."""

from __future__ import annotations

import pytest


def test_multi_page_tiff_ocr_is_capped(tmp_path, monkeypatch):
    pytest.importorskip("PIL")
    from PIL import Image
    from dragndoc.extractors import image as image_ext

    path = tmp_path / "scan.tiff"
    frames = [Image.new("RGB", (8, 8), color=(i, i, i)) for i in range(7)]
    frames[0].save(path, save_all=True, append_images=frames[1:])

    calls: list[str | None] = []

    def fake_ocr(_image, langs=None):
        calls.append(langs)
        return f"frame {len(calls)} text"

    monkeypatch.setattr(image_ext, "tesseract_available", lambda: True)
    monkeypatch.setattr(image_ext, "ocr_image", fake_ocr)

    doc = image_ext.extract(path)
    assert len(doc.sections) == 5
    assert doc.total_sections == 7
    assert [section.label for section in doc.sections] == [f"Page {i}" for i in range(1, 6)]
    assert doc.sections[-1].text == "frame 5 text"
    assert len(calls) == 5
    assert doc.ocr_used is True
    assert doc.ocr_decision == "ocr_pages"
