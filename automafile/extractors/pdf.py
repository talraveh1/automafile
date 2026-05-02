"""PDF extractor: pypdf for text. Embedded metadata via pikepdf (DocInfo + XMP)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from automafile.extractors._meta import collect
from automafile.extractors.base import (
    CorruptDocumentError,
    EncryptedDocumentError,
    ExtractedDoc,
)


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


def _per_page_chars(path: Path) -> list[int]:
    try:
        from pypdf import PdfReader
    except ImportError:
        return []
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            return []
        result = []
        for page in reader.pages:
            try:
                t = page.extract_text() or ""
            except Exception:
                t = ""
            result.append(len(t))
        return result
    except Exception:
        return []


def extract(path: Path) -> ExtractedDoc:
    metadata, encrypted = _read_pdf_metadata(path)
    if encrypted:
        raise EncryptedDocumentError(f"PDF is encrypted: {path}")

    text = ""
    per_page_chars: list[int] = []
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            raise EncryptedDocumentError(f"PDF is encrypted: {path}")
        chunks: list[str] = []
        for page in reader.pages:
            try:
                page_text = page.extract_text() or ""
            except Exception:
                page_text = ""
            chunks.append(page_text)
            per_page_chars.append(len(page_text.strip()))
        text = "\n".join(chunks)
    except EncryptedDocumentError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise CorruptDocumentError(f"pypdf failed for {path}: {exc}") from exc

    return ExtractedDoc(
        path=path,
        text=text,
        format="pdf",
        per_page_chars=per_page_chars,
        extracted_metadata=metadata,
    )


def per_page_char_counts(path: Path) -> list[int]:
    """Public helper used by the OCR decision logic."""
    return _per_page_chars(path)
