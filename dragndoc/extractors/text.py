"""Plain text extractor."""

from __future__ import annotations

from pathlib import Path

from dragndoc.config import get_settings
from dragndoc.extractors._caps import CapConfig, trim_to_word_boundary
from dragndoc.extractors._text_quality import raise_if_garbled
from dragndoc.extractors.base import CorruptDocumentError, ExtractedDoc, Section


def extract(path: Path, *, strict: bool = False) -> ExtractedDoc:
    cfg = CapConfig.from_settings(get_settings())
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8", errors="strict" if strict else "replace")
        raise_if_garbled(raw, text, path)
    except UnicodeDecodeError as exc:
        raise CorruptDocumentError(f"UTF-8 text decoding failed for {path}: {exc}") from exc
    except OSError as exc:
        raise CorruptDocumentError(f"Text reading failed for {path}: {exc}") from exc
    text = trim_to_word_boundary(text, cfg.target_chars)
    return ExtractedDoc(
        path=path,
        sections=[Section(label=None, text=text, index=0)],
        total_sections=None,
        format="text",
    )
