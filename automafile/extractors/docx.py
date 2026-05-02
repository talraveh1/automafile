"""DOCX extractor."""

from __future__ import annotations

from pathlib import Path

from automafile.extractors._meta import collect
from automafile.extractors.base import CorruptDocumentError, ExtractedDoc


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
    text = "\n".join(paragraphs)

    raw: dict = {}
    try:
        cp = doc.core_properties
        for attr in _CORE_ATTRS:
            raw[attr] = getattr(cp, attr, None)
    except Exception:
        pass

    return ExtractedDoc(
        path=path,
        text=text,
        format="docx",
        extracted_metadata=collect(raw, prefix="core_"),
    )
