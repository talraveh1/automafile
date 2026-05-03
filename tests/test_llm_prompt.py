"""Tests for section-aware LLM prompt rendering."""

from __future__ import annotations

from automafile.extractors.base import ExtractedDoc, Section
from automafile.llm import _build_prompt, _render_sections


def test_render_single_unlabeled_section_as_bare_text(tmp_path):
    sections = [Section(label=None, text="plain body", index=0)]
    assert _render_sections(sections, None) == "plain body"


def test_render_labeled_pages_with_total_and_partial_marker():
    sections = [
        Section(label="Page 1", text="first", index=0),
        Section(label="Page 2", text="second", index=1),
    ]
    rendered = _render_sections(sections, 5)
    assert "--- Page 1 of 5 ---\nfirst" in rendered
    assert "--- Page 2 of 5 ---\nsecond" in rendered
    assert "--- (showing pages 1-2 of 5) ---" in rendered


def test_render_sheet_label_includes_position():
    sections = [Section(label="Sheet: Invoices", text="rows", index=0)]
    assert _render_sections(sections, 3).startswith("--- Sheet: Invoices (1 of 3) ---")


def test_build_prompt_does_not_slice_rendered_text(tmp_path):
    doc = ExtractedDoc(
        path=tmp_path / "long.txt",
        sections=[Section(label=None, text="x" * 7000, index=0)],
        format="text",
    )
    prompt = _build_prompt(doc, {}, ["Unknown"])
    assert "x" * 6500 in prompt
