"""HTML extractor."""

from __future__ import annotations

from pathlib import Path

from automafile.extractors.base import CorruptDocumentError, ExtractedDoc


def extract(path: Path) -> ExtractedDoc:
    try:
        from bs4 import BeautifulSoup
    except ImportError as exc:
        raise CorruptDocumentError("beautifulsoup4 is not installed") from exc

    raw = path.read_bytes()
    soup = BeautifulSoup(raw, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text(separator="\n").strip()
    metadata: dict = {}
    if soup.title and soup.title.string:
        metadata["title"] = soup.title.string.strip()
    for meta in soup.find_all("meta"):
        name = meta.get("name") or meta.get("property")
        content = meta.get("content")
        if name and content:
            metadata[f"meta_{name}"] = content.strip()
    return ExtractedDoc(
        path=path,
        text=text,
        native_metadata=metadata,
        format="html",
        supports_native_metadata=False,
    )
