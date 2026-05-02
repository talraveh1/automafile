"""Common types shared by all extractors."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ExtractedDoc:
    """Uniform extractor output passed to the enrichment + writer pipeline."""

    path: Path
    text: str = ""
    ocr_used: bool = False
    ocr_decision: str = "no_ocr"
    ocr_pages: list[int] | None = None
    format: str = "unknown"
    error: str | None = None
    # PDF only: per-page character counts from the text-layer pass; lets the
    # OCR decision reuse extract()'s parse instead of opening the PDF a second time
    per_page_chars: list[int] | None = None
    # Flat key-value dict of metadata the file itself declares (PDF DocInfo +
    # XMP, OOXML core properties, EXIF, HTML <meta>, EPUB Dublin Core, ...).
    # Cleaned and length-clipped by ``extractors._meta.collect``. Surfaced to
    # the LLM as soft context.
    extracted_metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def has_text(self) -> bool:
        return bool(self.text and self.text.strip())


class ExtractorError(Exception):
    """Base exception for extractor failures."""


class EncryptedDocumentError(ExtractorError):
    """Raised when the source file is encrypted and cannot be opened."""


class CorruptDocumentError(ExtractorError):
    """Raised when the source file cannot be parsed."""
