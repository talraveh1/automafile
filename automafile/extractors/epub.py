"""EPUB extractor."""

from __future__ import annotations

from pathlib import Path

from automafile.extractors.base import CorruptDocumentError, ExtractedDoc


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

    metadata: dict = {}
    try:
        for ns_key in ("DC",):
            for k in ("title", "creator", "subject", "description", "language"):
                vals = book.get_metadata(ns_key, k) if hasattr(book, "get_metadata") else []
                if vals:
                    metadata[k] = vals[0][0]
    except Exception:
        pass

    return ExtractedDoc(
        path=path,
        text="\n\n".join(chunks),
        native_metadata=metadata,
        format="epub",
        supports_native_metadata=False,
    )
