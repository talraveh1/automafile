"""OCR decision logic and Tesseract wrapper."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from dragndoc.config import get_settings
from dragndoc.extractors.base import EncryptedDocumentError
from dragndoc.log import get_logger


log = get_logger(__name__)


OcrAction = Literal["ocr_full", "ocr_pages", "no_ocr", "skip_encrypted"]


@dataclass
class OcrDecision:
    action: OcrAction
    pages: list[int] = field(default_factory=list)
    reason: str = ""


def _resolve_tesseract_bin() -> str | None:
    settings = get_settings()
    # honor the configured path only if it actually exists; otherwise fall
    # through (this matters when running the host's config.jsonc inside a
    # Linux container where the Windows-style path is invalid)
    if settings.tesseract.bin and Path(settings.tesseract.bin).exists():
        return settings.tesseract.bin
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
    tessdata = get_settings().tesseract.prefix or os.environ.get("TESSDATA_PREFIX")
    if tessdata and Path(tessdata).exists():
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


def should_ocr_page(text: str) -> bool:
    settings = get_settings()
    return len((text or "").strip()) < settings.ocr.min_page_chars


def ocr_image(image, langs: str | None = None) -> str:
    settings = get_settings()
    langs = langs or settings.tesseract.langs
    bin_path = _configure_pytesseract()
    if not bin_path:
        raise RuntimeError("Tesseract is not installed or configured.")

    import pytesseract

    return pytesseract.image_to_string(image, lang=langs)


def pdf_ocr_decision(path: Path, per_page_chars: list[int] | None = None) -> OcrDecision:
    """Decide whether a PDF needs OCR and over which pages.

    If ``per_page_chars`` is supplied, skip the pypdf text-count pass.
    """
    settings = get_settings()
    if per_page_chars is None:
        # use pikepdf as the encryption oracle — its PasswordError is
        # unambiguous, unlike pypdf which raises DependencyError when it can't
        # verify AES keys without the `cryptography` package
        try:
            import pikepdf
            with pikepdf.open(path):
                pass
        except pikepdf.PasswordError:
            return OcrDecision(action="skip_encrypted", reason="pdf_encrypted")
        except Exception:  # noqa: BLE001
            pass

        try:
            from pypdf import PdfReader
            reader = PdfReader(str(path))
        except Exception as exc:  # noqa: BLE001
            return OcrDecision(action="ocr_full", reason=f"pypdf_failed: {exc}")
        per_page_chars = []
        for page in reader.pages:
            try:
                t = page.extract_text() or ""
            except Exception:
                t = ""
            per_page_chars.append(len(t.strip()))

    total = sum(per_page_chars)
    if total < settings.ocr.min_text_chars:
        log.debug("pdf_ocr_decision %s: ocr_full (no_text_layer; %d total chars)", path.name, total)
        return OcrDecision(action="ocr_full", reason="no_text_layer")

    sparse = [i for i, c in enumerate(per_page_chars) if c < settings.ocr.min_page_chars]
    if not sparse:
        log.debug("pdf_ocr_decision %s: no_ocr (%d pages, %d chars)", path.name, len(per_page_chars), total)
        return OcrDecision(action="no_ocr")
    if len(sparse) <= max(1, int(len(per_page_chars) * settings.ocr.sparse_page_ratio)):
        log.debug("pdf_ocr_decision %s: ocr_pages (%d sparse of %d)", path.name, len(sparse), len(per_page_chars))
        return OcrDecision(action="ocr_pages", pages=sparse, reason="sparse_pages")
    log.debug("pdf_ocr_decision %s: ocr_full (majority sparse, %d/%d)", path.name, len(sparse), len(per_page_chars))
    return OcrDecision(action="ocr_full", reason="majority_sparse")


def _coalesce_page_ranges(zero_based_pages: list[int]) -> list[tuple[int, int]]:
    """Collapse a sorted page-index list into ``(first_one_based, last_one_based)`` runs."""
    if not zero_based_pages:
        return []
    pages = sorted(set(zero_based_pages))
    runs: list[tuple[int, int]] = []
    run_start = pages[0]
    prev = run_start
    for p in pages[1:]:
        if p == prev + 1:
            prev = p
            continue
        runs.append((run_start + 1, prev + 1))
        run_start = p
        prev = p
    runs.append((run_start + 1, prev + 1))
    return runs


def run_ocr(path: Path, langs: str | None = None, pages: list[int] | None = None) -> str:
    """Run OCR on an image or PDF and return concatenated text."""
    import time as _time
    settings = get_settings()
    langs = langs or settings.tesseract.langs
    bin_path = _configure_pytesseract()
    if not bin_path:
        raise RuntimeError("Tesseract is not installed or configured.")

    import pytesseract

    suffix = path.suffix.lower()
    image_exts = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp", ".heic", ".heif"}
    started = _time.perf_counter()
    log.info("ocr starting: %s (langs=%s%s)", path.name, langs, f", pages={pages}" if pages else "")
    if suffix in image_exts:
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
        except Exception:
            pass
        from PIL import Image
        with Image.open(path) as im:
            text = ocr_image(im, langs=langs)
        log.info("ocr done: %s -> %d chars in %dms", path.name, len(text), int((_time.perf_counter() - started) * 1000))
        return text

    if suffix == ".pdf":
        try:
            from pdf2image import convert_from_path
        except ImportError as exc:
            raise RuntimeError("pdf2image is not installed") from exc
        if pages:
            # coalesce contiguous page indices into ranges so poppler is invoked
            # once per run of pages instead of once per page
            chunks: list[str] = []
            for first_one_based, last_one_based in _coalesce_page_ranges(pages):
                log.debug("ocr pdf range %s: pages %d-%d", path.name, first_one_based, last_one_based)
                imgs = convert_from_path(
                    str(path),
                    dpi=200,
                    first_page=first_one_based,
                    last_page=last_one_based,
                )
                for img in imgs:
                    chunks.append(pytesseract.image_to_string(img, lang=langs))
            text = "\n".join(chunks)
            log.info("ocr done: %s -> %d chars in %dms (%d page range(s))", path.name, len(text), int((_time.perf_counter() - started) * 1000), len(_coalesce_page_ranges(pages)))
            return text
        imgs = convert_from_path(str(path), dpi=200)
        log.debug("ocr pdf full %s: %d pages rendered", path.name, len(imgs))
        text = "\n".join(pytesseract.image_to_string(img, lang=langs) for img in imgs)
        log.info("ocr done: %s -> %d chars in %dms (%d pages)", path.name, len(text), int((_time.perf_counter() - started) * 1000), len(imgs))
        return text

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
