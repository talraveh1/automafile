"""OCR decision logic and Tesseract wrapper."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from automafile.config import get_settings
from automafile.extractors.base import EncryptedDocumentError
from automafile.log import get_logger


log = get_logger(__name__)


OcrAction = Literal["ocr_full", "ocr_pages", "no_ocr", "skip_encrypted"]


@dataclass
class OcrDecision:
    action: OcrAction
    pages: list[int] = field(default_factory=list)
    reason: str = ""


def _resolve_tesseract_bin() -> str | None:
    settings = get_settings()
    if settings.tesseract_bin:
        return settings.tesseract_bin
    found = shutil.which("tesseract")
    if found:
        return found
    candidates = [
        Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe"),
        Path(r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"),
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return None


def _configure_pytesseract() -> str | None:
    bin_path = _resolve_tesseract_bin()
    if bin_path:
        try:
            import pytesseract
            pytesseract.pytesseract.tesseract_cmd = bin_path
        except ImportError:
            return None
    tessdata = get_settings().tessdata_prefix or os.environ.get("TESSDATA_PREFIX")
    if tessdata:
        os.environ["TESSDATA_PREFIX"] = tessdata
    return bin_path


def _tess_command_with_env(args: list[str]) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    return subprocess.run(args, capture_output=True, text=True, check=False, env=env)


def tesseract_available() -> bool:
    return _resolve_tesseract_bin() is not None


def tesseract_version() -> str:
    bin_path = _configure_pytesseract()
    if not bin_path:
        return "unknown"
    try:
        import pytesseract
        v = pytesseract.get_tesseract_version()
        return f"tesseract {v}"
    except Exception:
        try:
            out = subprocess.run(
                [bin_path, "--version"],
                capture_output=True, text=True, check=False,
            )
            first = (out.stdout or out.stderr).splitlines()[0] if out.stdout or out.stderr else ""
            return first.strip() or "tesseract unknown"
        except Exception:
            return "tesseract unknown"


def tesseract_languages() -> list[str]:
    bin_path = _resolve_tesseract_bin()
    if not bin_path:
        return []
    try:
        _configure_pytesseract()
        out = _tess_command_with_env([bin_path, "--list-langs"])
        text = (out.stdout or "") + (out.stderr or "")
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        return [ln for ln in lines if not ln.lower().startswith("list of")]
    except Exception:
        return []


def pdf_ocr_decision(path: Path) -> OcrDecision:
    """Decide whether a PDF needs OCR and over which pages."""
    settings = get_settings()
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(path))
    except Exception as exc:  # noqa: BLE001
        if "encrypt" in str(exc).lower():
            return OcrDecision(action="skip_encrypted", reason="pdf_encrypted")
        return OcrDecision(action="ocr_full", reason=f"pypdf_failed: {exc}")
    if reader.is_encrypted:
        return OcrDecision(action="skip_encrypted", reason="pdf_encrypted")

    per_page: list[int] = []
    for page in reader.pages:
        try:
            t = page.extract_text() or ""
        except Exception:
            t = ""
        per_page.append(len(t.strip()))

    total = sum(per_page)
    if total < settings.ocr_min_text_chars:
        return OcrDecision(action="ocr_full", reason="no_text_layer")

    sparse = [i for i, c in enumerate(per_page) if c < settings.ocr_min_page_chars]
    if not sparse:
        return OcrDecision(action="no_ocr")
    if len(sparse) <= max(1, int(len(per_page) * settings.ocr_sparse_page_ratio)):
        return OcrDecision(action="ocr_pages", pages=sparse, reason="sparse_pages")
    return OcrDecision(action="ocr_full", reason="majority_sparse")


def run_ocr(path: Path, langs: str | None = None, pages: list[int] | None = None) -> str:
    """Run OCR on an image or PDF and return concatenated text."""
    settings = get_settings()
    langs = langs or settings.tesseract_langs
    bin_path = _configure_pytesseract()
    if not bin_path:
        raise RuntimeError("Tesseract is not installed or configured.")

    import pytesseract

    suffix = path.suffix.lower()
    image_exts = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp", ".heic", ".heif"}
    if suffix in image_exts:
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
        except Exception:
            pass
        from PIL import Image
        with Image.open(path) as im:
            return pytesseract.image_to_string(im, lang=langs)

    if suffix == ".pdf":
        try:
            from pdf2image import convert_from_path
        except ImportError as exc:
            raise RuntimeError("pdf2image is not installed") from exc
        if pages:
            chunks: list[str] = []
            for one_based_page in (p + 1 for p in pages):
                imgs = convert_from_path(
                    str(path),
                    dpi=200,
                    first_page=one_based_page,
                    last_page=one_based_page,
                )
                for img in imgs:
                    chunks.append(pytesseract.image_to_string(img, lang=langs))
            return "\n".join(chunks)
        imgs = convert_from_path(str(path), dpi=200)
        return "\n".join(pytesseract.image_to_string(img, lang=langs) for img in imgs)

    raise ValueError(f"Unsupported file type for OCR: {suffix}")


def record_ocr_metadata(meta: dict, langs: str, decision: str) -> dict:
    """Fill the ``ocr`` block in a sidecar/native metadata dict."""
    from datetime import datetime, timezone
    meta = dict(meta)
    meta["ocr"] = {
        "decision": decision,
        "done_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "engine": "tesseract",
        "engine_version": tesseract_version(),
        "languages": langs,
    }
    return meta
