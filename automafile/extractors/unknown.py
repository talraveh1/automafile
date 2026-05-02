"""Catch-all extractor for unrecognized file types."""

from __future__ import annotations

from pathlib import Path

from automafile.extractors.base import ExtractedDoc


def extract(path: Path) -> ExtractedDoc:
    text = ""
    try:
        raw = path.read_bytes()[:8192]
        text = raw.decode("utf-8", errors="replace") if raw else ""
        if "\x00" in text:
            text = ""
    except Exception:
        text = ""
    return ExtractedDoc(
        path=path,
        text=text,
        format="unknown",
    )
