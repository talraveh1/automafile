"""Dispatch a path to the correct extractor based on extension or MIME."""

from __future__ import annotations

from pathlib import Path

from automafile.extractors import (
    docx as docx_ext,
    epub as epub_ext,
    html as html_ext,
    image as image_ext,
    pdf as pdf_ext,
    pptx as pptx_ext,
    text as text_ext,
    unknown as unknown_ext,
    xlsx as xlsx_ext,
)
from automafile.extractors.base import ExtractedDoc
from automafile.log import get_logger


log = get_logger(__name__)


EXT_MAP = {
    ".pdf": pdf_ext,
    ".docx": docx_ext,
    ".xlsx": xlsx_ext,
    ".pptx": pptx_ext,
    ".jpg": image_ext,
    ".jpeg": image_ext,
    ".png": image_ext,
    ".gif": image_ext,
    ".bmp": image_ext,
    ".tif": image_ext,
    ".tiff": image_ext,
    ".webp": image_ext,
    ".heic": image_ext,
    ".heif": image_ext,
    ".html": html_ext,
    ".htm": html_ext,
    ".epub": epub_ext,
    ".txt": text_ext,
    ".md": text_ext,
    ".markdown": text_ext,
    ".csv": text_ext,
    ".log": text_ext,
    ".json": text_ext,
    ".xml": text_ext,
    ".yaml": text_ext,
    ".yml": text_ext,
}

MIME_MAP = {
    "application/pdf": pdf_ext,
    "image/jpeg": image_ext,
    "image/png": image_ext,
    "image/gif": image_ext,
    "image/heic": image_ext,
    "image/heif": image_ext,
    "image/bmp": image_ext,
    "image/tiff": image_ext,
    "image/webp": image_ext,
    "text/plain": text_ext,
    "text/html": html_ext,
    "application/epub+zip": epub_ext,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": docx_ext,
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": xlsx_ext,
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": pptx_ext,
}


def _sniff_mime(path: Path) -> str | None:
    try:
        import magic  # type: ignore
    except ImportError:
        return None
    try:
        return magic.from_file(str(path), mime=True)
    except Exception:
        return None


def get_extractor(path: Path):
    ext = path.suffix.lower()
    if ext in EXT_MAP:
        mod = EXT_MAP[ext]
        log.debug("extractor for %s: %s (by extension)", path.name, mod.__name__)
        return mod
    mime = _sniff_mime(path)
    if mime and mime in MIME_MAP:
        mod = MIME_MAP[mime]
        log.debug("extractor for %s: %s (by mime %s)", path.name, mod.__name__, mime)
        return mod
    log.debug("extractor for %s: unknown (ext=%s mime=%s)", path.name, ext, mime)
    return unknown_ext


def extract(path: Path) -> ExtractedDoc:
    extractor = get_extractor(path)
    return extractor.extract(path)
