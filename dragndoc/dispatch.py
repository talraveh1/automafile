"""Dispatch a path to the correct extractor using extension and MIME fallbacks."""

from __future__ import annotations

from pathlib import Path

from dragndoc.extractors import (
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
from dragndoc.extractors.base import CorruptDocumentError, ExtractedDoc, ExtractorError
from dragndoc.log import get_logger


log = get_logger(__name__)


UNKNOWN_MIME = "unknown"

EXT_MIME_MAP = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".webp": "image/webp",
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".html": "text/html",
    ".htm": "text/html",
    ".epub": "application/epub+zip",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".csv": "text/csv",
    ".log": "text/plain",
    ".json": "application/json",
    ".xml": "application/xml",
    ".yaml": "application/x-yaml",
    ".yml": "application/x-yaml",
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
    "text/csv": text_ext,
    "text/markdown": text_ext,
    "text/html": html_ext,
    "application/json": text_ext,
    "application/xml": text_ext,
    "text/xml": text_ext,
    "application/yaml": text_ext,
    "application/x-yaml": text_ext,
    "application/epub+zip": epub_ext,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": docx_ext,
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": xlsx_ext,
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": pptx_ext,
}
EXT_MAP = {
    ext: MIME_MAP[mime]
    for ext, mime in EXT_MIME_MAP.items()
    if mime in MIME_MAP
}
_STRICT_AWARE_EXTRACTORS = {text_ext, html_ext, unknown_ext}


def _sniff_mime(path: Path) -> str | None:
    try:
        import magic  # type: ignore
    except ImportError:
        return None
    try:
        return magic.from_file(str(path), mime=True)
    except Exception:
        return None


def _mime_from_extension(path: Path) -> str:
    return EXT_MIME_MAP.get(path.suffix.lower(), UNKNOWN_MIME)


def _extractor_for_mime(mime: str):
    if mime == UNKNOWN_MIME:
        return unknown_ext
    return MIME_MAP.get(mime)


def _extract_with_mime(path: Path, mime: str, *, strict: bool) -> ExtractedDoc:
    extractor = _extractor_for_mime(mime)
    if extractor is None:
        raise CorruptDocumentError(f"Unsupported MIME type for extraction: {mime}")
    if extractor in _STRICT_AWARE_EXTRACTORS:
        # only text-like extractors currently distinguish strict from fallback mode
        return extractor.extract(path, strict=strict)
    return extractor.extract(path)


def get_extractor(path: Path):
    ext_mime = _mime_from_extension(path)
    if ext_mime != UNKNOWN_MIME:
        mod = _extractor_for_mime(ext_mime) or unknown_ext
        log.debug("extractor for %s: %s (by extension MIME %s)", path.name, mod.__name__, ext_mime)
        return mod
    mime = _sniff_mime(path)
    mod = _extractor_for_mime(mime or UNKNOWN_MIME)
    if mod is not None and mime:
        log.debug("extractor for %s: %s (by mime %s)", path.name, mod.__name__, mime)
        return mod
    log.debug("extractor for %s: unknown (ext=%s mime=%s)", path.name, path.suffix.lower(), mime)
    return unknown_ext


def extract(path: Path) -> ExtractedDoc:
    mime = _mime_from_extension(path)
    if mime != UNKNOWN_MIME:
        try:
            # known extensions get a strict first pass so mislabeled files fail fast
            doc = _extract_with_mime(path, mime, strict=True)
            log.debug("extracted %s by extension MIME %s", path.name, mime)
            return doc
        except ExtractorError as exc:
            log.debug("extension MIME extraction failed for %s as %s: %s", path.name, mime, exc)

    # fall back to sniffing only after the extension-based path declines the file
    sniffed_mime = _sniff_mime(path)
    if sniffed_mime:
        mime = sniffed_mime
        log.debug("sniffed %s as %s", path.name, mime)

    if mime != UNKNOWN_MIME:
        try:
            doc = _extract_with_mime(path, mime, strict=False)
            log.debug("extracted %s by sniffed/fallback MIME %s", path.name, mime)
            return doc
        except ExtractorError as exc:
            log.debug("fallback MIME extraction failed for %s as %s: %s", path.name, mime, exc)

    # the unknown extractor is the final safety net for unsupported or ambiguous files
    log.debug("extracting %s as unknown", path.name)
    return _extract_with_mime(path, UNKNOWN_MIME, strict=False)
