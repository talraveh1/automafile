"""Catch-all extractor for unrecognized file types."""

from __future__ import annotations

from pathlib import Path

from automafile.config import get_settings
from automafile.extractors._caps import CapConfig, trim_to_word_boundary
from automafile.extractors._text_quality import looks_binary_or_garbled
from automafile.extractors.base import ExtractedDoc, Section


def extract(path: Path, *, strict: bool = False) -> ExtractedDoc:
    cfg = CapConfig.from_settings(get_settings())
    text = ""
    try:
        raw = path.read_bytes()[:8192]
        text = raw.decode("utf-8", errors="replace") if raw else ""
        if looks_binary_or_garbled(raw, text):
            text = ""
    except Exception:
        text = ""
    text = trim_to_word_boundary(text, cfg.target_chars)
    return ExtractedDoc(
        path=path,
        sections=[Section(label=None, text=text, index=0)],
        total_sections=None,
        format="unknown",
    )
