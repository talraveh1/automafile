"""Plain text extractor."""

from __future__ import annotations

from pathlib import Path

from automafile.extractors.base import ExtractedDoc


def extract(path: Path) -> ExtractedDoc:
    text = path.read_text(encoding="utf-8", errors="replace")
    return ExtractedDoc(
        path=path,
        text=text,
        format="text",
    )
