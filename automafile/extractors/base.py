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
    native_metadata: dict[str, Any] = field(default_factory=dict)
    ocr_used: bool = False
    ocr_decision: str = "no_ocr"
    ocr_pages: list[int] | None = None
    format: str = "unknown"
    supports_native_metadata: bool = False
    error: str | None = None
    # PDF only: per-page character counts from the text-layer pass; lets the
    # OCR decision reuse extract()'s parse instead of opening the PDF a second time
    per_page_chars: list[int] | None = None

    @property
    def has_text(self) -> bool:
        return bool(self.text and self.text.strip())


class ExtractorError(Exception):
    """Base exception for extractor failures."""


class EncryptedDocumentError(ExtractorError):
    """Raised when the source file is encrypted and cannot be opened."""


class CorruptDocumentError(ExtractorError):
    """Raised when the source file cannot be parsed."""
