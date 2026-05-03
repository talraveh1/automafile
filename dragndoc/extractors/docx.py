"""DOCX extractor."""

from __future__ import annotations

from pathlib import Path

from automafile.config import get_settings
from automafile.extractors._caps import CapConfig, trim_to_word_boundary
from automafile.extractors._meta import collect
from automafile.extractors.base import CorruptDocumentError, ExtractedDoc, Section


# python-docx CoreProperties attributes — these are the OOXML core
# properties Windows 11 exposes in the Details pane.
_CORE_ATTRS = (
    "title", "author", "subject", "keywords", "category", "comments",
    "last_modified_by", "revision", "version", "created", "modified",
    "last_printed", "content_status", "identifier", "language",
)


def extract(path: Path) -> ExtractedDoc:
    try:
        from docx import Document
    except ImportError as exc:
        raise CorruptDocumentError("python-docx is not installed") from exc
    try:
        doc = Document(str(path))
    except Exception as exc:  # noqa: BLE001
        raise CorruptDocumentError(f"docx failed for {path}: {exc}") from exc

    paragraphs = [p.text for p in doc.paragraphs if p.text]
    cfg = CapConfig.from_settings(get_settings())
    # docx pagination is layout-dependent; heading pseudo-sections are a future improvement
    text = trim_to_word_boundary("\n".join(paragraphs), cfg.target_chars)

    raw: dict = {}
    try:
        cp = doc.core_properties
        for attr in _CORE_ATTRS:
            raw[attr] = getattr(cp, attr, None)
    except Exception:
        pass

    return ExtractedDoc(
        path=path,
        sections=[Section(label=None, text=text, index=0)],
        total_sections=None,
        format="docx",
        extracted_metadata=collect(raw, prefix="core_"),
    )
