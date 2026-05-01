"""DOCX extractor."""

from __future__ import annotations

from pathlib import Path

from automafile.extractors.base import CorruptDocumentError, ExtractedDoc


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

    metadata: dict = {}
    try:
        cp = doc.core_properties
        for attr in ("title", "subject", "keywords", "category", "comments", "author"):
            v = getattr(cp, attr, None)
            if v:
                metadata[attr] = v
    except Exception:
        pass

    return ExtractedDoc(
        path=path,
        text=text,
        native_metadata=metadata,
        format="docx",
        supports_native_metadata=True,
    )
