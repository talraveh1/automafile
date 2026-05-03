"""PDF extractor: pypdf for text. Embedded metadata via pikepdf (DocInfo + XMP)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from automafile.config import get_settings
from automafile.extractors._caps import CapConfig, select_pages
from automafile.extractors._meta import collect
from automafile.extractors.base import (
    CorruptDocumentError,
    EncryptedDocumentError,
    ExtractedDoc,
    Section,
)
from automafile.log import get_logger
from automafile.ocr import run_ocr, should_ocr_page, tesseract_available


log = get_logger(__name__)

_XMP_NS_PREFIX = {
    "http://purl.org/dc/elements/1.1/": "dc",
    "http://ns.adobe.com/xap/1.0/": "xmp",
    "http://ns.adobe.com/xap/1.0/mm/": "xmpMM",
    "http://ns.adobe.com/xap/1.0/rights/": "xmpRights",
    "http://ns.adobe.com/pdf/1.3/": "pdf",
    "http://ns.adobe.com/pdfx/1.3/": "pdfx",
    "http://ns.adobe.com/photoshop/1.0/": "photoshop",
    "http://www.w3.org/1999/02/22-rdf-syntax-ns#": "rdf",
    "http://automafile.local/schema/1.0/": "automafile",
}


def _normalize_xmp_key(k: str) -> str:
    """Turn ``{namespace}localname`` Clark notation into ``prefix:localname``.

    Falls back to ``xmp_<localname>`` when the namespace isn't recognized,
    rather than emitting an unwieldy URL-bearing key.
    """
    if not (k.startswith("{") and "}" in k):
        return k
    ns, local = k[1:].split("}", 1)
    prefix = _XMP_NS_PREFIX.get(ns)
    return f"{prefix}:{local}" if prefix else f"xmp_{local}"


def _read_pdf_metadata(path: Path) -> tuple[dict[str, Any], bool]:
    """Return ``(extracted_metadata, is_encrypted)``.

    Combines the legacy ``/Info`` dictionary (under ``info_<Key>`` keys) with
    every XMP property pikepdf exposes (normalized to ``prefix:localname``,
    e.g. ``dc:title``, ``xmp:CreateDate``, ``pdf:Producer``).
    """
    try:
        import pikepdf
    except ImportError:
        return {}, False
    try:
        with pikepdf.open(path) as pdf:
            raw: dict[str, Any] = {}
            try:
                for k, v in pdf.docinfo.items():
                    raw[f"info_{str(k).lstrip('/')}"] = str(v)
            except Exception:
                pass
            try:
                with pdf.open_metadata() as xmp:
                    for k, v in dict(xmp).items():
                        key = _normalize_xmp_key(str(k))
                        raw[key] = v if isinstance(v, list) else str(v)
            except Exception:
                pass
            return collect(raw), False
    except pikepdf.PasswordError:
        return {}, True
    except Exception as exc:  # noqa: BLE001
        raise CorruptDocumentError(f"pikepdf could not open {path}: {exc}") from exc


def extract(path: Path) -> ExtractedDoc:
    metadata, encrypted = _read_pdf_metadata(path)
    if encrypted:
        raise EncryptedDocumentError(f"PDF is encrypted: {path}")

    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        if reader.is_encrypted:
            raise EncryptedDocumentError(f"PDF is encrypted: {path}")
    except EncryptedDocumentError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise CorruptDocumentError(f"pypdf failed for {path}: {exc}") from exc

    settings = get_settings()
    cfg = CapConfig.from_settings(settings)
    text_layer_chars: list[int] = []
    ocr_pages: list[int] = []
    ocr_unavailable = False
    ocr_failed = False

    def _iter_pages():
        nonlocal ocr_failed, ocr_unavailable
        for page_index, page in enumerate(reader.pages):
            try:
                page_text = page.extract_text() or ""
            except Exception:
                page_text = ""
            text_layer_chars.append(len(page_text.strip()))

            section_text = page_text
            if should_ocr_page(page_text):
                if not tesseract_available():
                    ocr_unavailable = True
                else:
                    try:
                        section_text = run_ocr(path, langs=settings.tesseract_langs, pages=[page_index])
                        ocr_pages.append(page_index)
                    except Exception as exc:  # noqa: BLE001
                        ocr_failed = True
                        log.warning("OCR failed for %s page %d: %s", path, page_index + 1, exc)
            yield section_text

    kept = select_pages(_iter_pages(), cfg)
    sections = [
        Section(label=f"Page {i + 1}", text=text, index=i)
        for i, text in enumerate(kept)
    ]

    if ocr_pages:
        ocr_decision = "ocr_pages"
    elif ocr_failed:
        ocr_decision = "ocr_failed"
    elif ocr_unavailable:
        ocr_decision = "ocr_unavailable"
    else:
        ocr_decision = "no_ocr"

    return ExtractedDoc(
        path=path,
        sections=sections,
        total_sections=len(reader.pages),
        ocr_used=bool(ocr_pages),
        ocr_decision=ocr_decision,
        ocr_pages=ocr_pages or None,
        format="pdf",
        per_page_chars=text_layer_chars,
        extracted_metadata=metadata,
    )
