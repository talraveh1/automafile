"""PDF extractor: pypdf for text + pikepdf for metadata."""

from __future__ import annotations

from pathlib import Path

from automafile.extractors.base import (
    CorruptDocumentError,
    EncryptedDocumentError,
    ExtractedDoc,
)


def _read_pikepdf_metadata(path: Path) -> tuple[dict, bool]:
    """Return (metadata-dict, is_encrypted)."""
    try:
        import pikepdf
    except ImportError:
        return {}, False
    try:
        with pikepdf.open(path) as pdf:
            info = {}
            try:
                docinfo = pdf.docinfo
                for k, v in docinfo.items():
                    info[str(k).lstrip("/")] = str(v)
            except Exception:
                pass
            try:
                with pdf.open_metadata() as xmp:
                    for k, v in dict(xmp).items():
                        info[k] = str(v)
            except Exception:
                pass
            return info, False
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
    metadata, encrypted = _read_pikepdf_metadata(path)
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
        native_metadata=metadata,
        format="pdf",
        supports_native_metadata=True,
        per_page_chars=per_page_chars,
    )


def per_page_char_counts(path: Path) -> list[int]:
    """Public helper used by the OCR decision logic."""
    return _per_page_chars(path)
