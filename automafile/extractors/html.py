"""HTML extractor."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from automafile.extractors._meta import collect
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

    raw_meta: dict[str, Any] = {}
    if soup.title and soup.title.string:
        raw_meta["title"] = soup.title.string
    for tag in soup.find_all("meta"):
        name = tag.get("name") or tag.get("property") or tag.get("http-equiv")
        content = tag.get("content")
        if name and content:
            raw_meta[f"meta_{name}"] = content
    if getattr(soup, "html", None) is not None:
        lang = soup.html.get("lang") if hasattr(soup.html, "get") else None
        if lang:
            raw_meta["html_lang"] = lang

    return ExtractedDoc(
        path=path,
        text=text,
        format="html",
        extracted_metadata=collect(raw_meta),
    )
