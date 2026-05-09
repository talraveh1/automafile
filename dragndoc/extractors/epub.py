"""EPUB extractor."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from dragndoc.config import get_settings
from dragndoc.extractors._caps import CapConfig, select_pages
from dragndoc.extractors._meta import collect
from dragndoc.extractors.base import CorruptDocumentError, ExtractedDoc, Section


# full Dublin Core element set recommended by the EPUB spec
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

    cfg = CapConfig.from_settings(get_settings())
    items = list(book.get_items_of_type(ITEM_DOCUMENT))
    labels: list[str] = []

    def _iter_chapters():
        for i, item in enumerate(items):
            soup = BeautifulSoup(item.get_content(), "html.parser")
            for tag in soup(["script", "style"]):
                tag.decompose()
            title = soup.find(["h1", "title"])
            label = title.get_text(" ", strip=True) if title else ""
            labels.append(label or f"Chapter {i + 1}")
            yield soup.get_text(separator="\n").strip()

    kept = select_pages(_iter_chapters(), cfg)
    sections = [
        Section(label=labels[i], text=text, index=i)
        for i, text in enumerate(kept)
    ]

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
        sections=sections,
        total_sections=len(items),
        format="epub",
        extracted_metadata=collect(raw, prefix="dc_"),
    )
