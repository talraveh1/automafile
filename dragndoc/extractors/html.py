"""HTML extractor."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from dragndoc.config import get_settings
from dragndoc.extractors._caps import CapConfig, trim_to_word_boundary
from dragndoc.extractors._meta import collect
from dragndoc.extractors._text_quality import raise_if_garbled
from dragndoc.extractors.base import CorruptDocumentError, ExtractedDoc, Section


def extract(path: Path, *, strict: bool = False) -> ExtractedDoc:
    try:
        from bs4 import BeautifulSoup
    except ImportError as exc:
        raise CorruptDocumentError("Beautifulsoup4 is not installed") from exc

    try:
        raw = path.read_bytes()
        decoded = raw.decode("utf-8", errors="strict" if strict else "replace")
        raise_if_garbled(raw, decoded, path)
    except UnicodeDecodeError as exc:
        raise CorruptDocumentError(f"UTF-8 HTML decoding failed for {path}: {exc}") from exc
    except OSError as exc:
        raise CorruptDocumentError(f"HTML reading failed for {path}: {exc}") from exc

    soup = BeautifulSoup(decoded, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    cfg = CapConfig.from_settings(get_settings())
    text = trim_to_word_boundary(soup.get_text(separator="\n").strip(), cfg.target_chars)

    raw_meta: dict[str, Any] = {}
    if soup.title and soup.title.string:
        raw_meta["title"] = soup.title.string
    for tag in soup.find_all("meta"):
        name = tag.get("name") or tag.get("property") or tag.get("http-equiv")
        content = tag.get("content")
        if name and content:
            raw_meta[f"meta_{name}"] = content
    html_tag = getattr(soup, "html", None)
    if html_tag is not None and hasattr(html_tag, "get"):
        lang = html_tag.get("lang")
        if lang:
            raw_meta["html_lang"] = lang

    return ExtractedDoc(
        path=path,
        sections=[Section(label=None, text=text, index=0)],
        total_sections=None,
        format="html",
        extracted_metadata=collect(raw_meta),
    )
