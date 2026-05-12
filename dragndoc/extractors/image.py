"""Image extractor: text comes from OCR; metadata from EXIF + PIL info dict.

Surfaces every populated EXIF tag including GPSInfo and ExifIFD subblocks,
plus any non-binary entries in PIL's ``info`` dict (PNG iTXt chunks, etc.).
This is what Windows 11 reads to populate Photo properties (Date taken,
Camera maker, Camera model, F-stop, ExposureTime, ISO, GPS, ...).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from dragndoc.config import get_settings
from dragndoc.extractors._caps import CapConfig, select_pages
from dragndoc.extractors._meta import collect
from dragndoc.extractors._ocr import OcrTracker
from dragndoc.extractors.base import CorruptDocumentError, ExtractedDoc, Section
from dragndoc.log import get_logger
from dragndoc.ocr import ocr_image, tesseract_available


log = get_logger(__name__)


# exif sub-IFD tag IDs (PIL exposes these via Image.Exif.get_ifd)
_EXIF_IFD_TAG = 0x8769
_GPS_IFD_TAG = 0x8825


def _read_exif(im) -> dict[str, Any]:
    from PIL import ExifTags
    out: dict[str, Any] = {}
    try:
        exif = im.getexif() if hasattr(im, "getexif") else None
    except Exception:
        return out
    if not exif:
        return out

    for tag_id, value in exif.items():
        name = ExifTags.TAGS.get(tag_id, f"tag_{tag_id}")
        out[name] = value

    # exififd subblock — date taken, exposure, ISO, lens, etc
    try:
        sub = exif.get_ifd(_EXIF_IFD_TAG)
    except Exception:
        sub = None
    if sub:
        for tag_id, value in sub.items():
            name = ExifTags.TAGS.get(tag_id, f"tag_{tag_id}")
            out.setdefault(name, value)

    # gps subblock — coordinates, altitude, etc
    try:
        gps = exif.get_ifd(_GPS_IFD_TAG)
    except Exception:
        gps = None
    if gps:
        for tag_id, value in gps.items():
            name = ExifTags.GPSTAGS.get(tag_id, f"gps_tag_{tag_id}")
            out[f"GPS_{name}"] = value

    return out


def extract(path: Path) -> ExtractedDoc:
    try:
        from PIL import Image
    except ImportError as exc:
        raise CorruptDocumentError("Pillow is not installed") from exc

    try:
        import pillow_heif  # noqa: F401
        pillow_heif.register_heif_opener()
    except Exception:
        pass

    settings = get_settings()
    cfg = CapConfig.from_settings(settings)
    metadata: dict[str, Any] = {}
    n_frames = 1
    ocr = OcrTracker()
    try:
        with Image.open(path) as im:
            n_frames = int(getattr(im, "n_frames", 1) or 1)
            info = getattr(im, "info", {}) or {}
            metadata.update(collect(info, prefix="info_"))
            try:
                metadata["dimensions"] = f"{im.width}x{im.height}"
                metadata["color_mode"] = im.mode
                if getattr(im, "format", None):
                    metadata["pil_format"] = im.format
            except Exception:
                pass
            metadata.update(collect(_read_exif(im), prefix="exif_"))

            def _iter_frames():
                for frame_index in range(n_frames):
                    try:
                        im.seek(frame_index)
                    except EOFError:
                        break
                    frame_text = ""
                    # todo(vision): if OCR is sparse, optionally describe this frame with a vision model
                    if not tesseract_available():
                        ocr.unavailable = True
                    else:
                        try:
                            frame_text = ocr_image(im.copy(), langs=settings.tesseract.langs)
                            ocr.pages.append(frame_index)
                        except Exception as exc:  # noqa: BLE001
                            ocr.failed = True
                            log.warning("OCR failed for %s frame %d: %s", path, frame_index + 1, exc)
                    yield frame_text

            # multi-frame images are treated like page sequences for capping and labels
            kept = select_pages(_iter_frames(), cfg)
    except Exception as exc:  # noqa: BLE001
        raise CorruptDocumentError(f"Image failed for {path}: {exc}") from exc

    if n_frames == 1:
        sections = [Section(label=None, text=kept[0] if kept else "", index=0)]
        total_sections = None
    else:
        sections = [
            Section(label=f"Page {i + 1}", text=text, index=i)
            for i, text in enumerate(kept)
        ]
        total_sections = n_frames

    success_decision = "ocr_full" if n_frames == 1 else "ocr_pages"

    return ExtractedDoc(
        path=path,
        sections=sections,
        total_sections=total_sections,
        format="image",
        ocr_used=bool(ocr.pages),
        ocr_decision=ocr.decision(success=success_decision),
        ocr_pages=ocr.pages or None,
        extracted_metadata=metadata,
    )
