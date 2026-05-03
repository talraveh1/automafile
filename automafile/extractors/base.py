"""Common types shared by all extractors."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Section:
    """A bounded slice of extracted text from one logical document section."""

    label: str | None
    text: str
    index: int


@dataclass
class ExtractedDoc:
    """Uniform extractor output passed to the enrichment + writer pipeline."""

    path: Path
    sections: list[Section] = field(default_factory=list)
    total_sections: int | None = None
    text: str = field(init=False, default="")
    ocr_used: bool = False
    ocr_decision: str = "no_ocr"
    ocr_pages: list[int] | None = None
    format: str = "unknown"
    error: str | None = None
    # PDF only: full per-page character counts from the text-layer pass for
    # kept pages, before OCR and before section trimming
    per_page_chars: list[int] | None = None
    # Flat key-value dict of metadata the file itself declares (PDF DocInfo +
    # XMP, OOXML core properties, EXIF, HTML <meta>, EPUB Dublin Core, ...).
    # Cleaned and length-clipped by ``extractors._meta.collect``. Surfaced to
    # the LLM as soft context.
    extracted_metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.refresh_text()

    def refresh_text(self) -> None:
        self.text = "\n\n".join(section.text for section in self.sections)

    @property
    def has_text(self) -> bool:
        return bool(self.sections and any(section.text.strip() for section in self.sections))


class ExtractorError(Exception):
    """Base exception for extractor failures."""


class EncryptedDocumentError(ExtractorError):
    """Raised when the source file is encrypted and cannot be opened."""


class CorruptDocumentError(ExtractorError):
    """Raised when the source file cannot be parsed."""
