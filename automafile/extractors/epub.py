"""EPUB extractor."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from automafile.extractors._meta import collect
from automafile.extractors.base import CorruptDocumentError, ExtractedDoc


# Full Dublin Core element set (the EPUB spec recommends these).
_DC_FIELDS = (
    "title", "creator", "subject", "description", "publisher",
    "contributor", "date", "type", "format", "identifier",
    "source", "language", "relation", "coverage", "rights",
)


def extract(path: Path) -> ExtractedDoc:
    try:
        from ebooklib import epub, ITEM_DOCUMENT
        from bs4 import BeautifulSoup
    except ImportError as exc:
        raise CorruptDocumentError("ebooklib / beautifulsoup4 not installed") from exc

    try:
        book = epub.read_epub(str(path))
    except Exception as exc:  # noqa: BLE001
        raise CorruptDocumentError(f"epub failed: {exc}") from exc

    chunks: list[str] = []
    for item in book.get_items_of_type(ITEM_DOCUMENT):
        soup = BeautifulSoup(item.get_content(), "html.parser")
        for tag in soup(["script", "style"]):
            tag.decompose()
        chunks.append(soup.get_text(separator="\n").strip())

    raw: dict[str, Any] = {}
    if hasattr(book, "get_metadata"):
        for field_name in _DC_FIELDS:
            try:
                vals = book.get_metadata("DC", field_name) or []
            except Exception:
                vals = []
            if not vals:
                continue
            # ebooklib returns a list of (value, attribs) tuples; flatten to
            # values only, preserving multiple authors/subjects/etc.
            extracted: list[str] = []
            for entry in vals:
                v = entry[0] if isinstance(entry, tuple) else entry
                if v:
                    extracted.append(str(v))
            if not extracted:
                continue
            raw[field_name] = extracted if len(extracted) > 1 else extracted[0]

    return ExtractedDoc(
        path=path,
        text="\n\n".join(chunks),
        format="epub",
        extracted_metadata=collect(raw, prefix="dc_"),
    )
