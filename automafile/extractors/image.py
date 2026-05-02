"""Image extractor: text comes from OCR; metadata from EXIF + PIL info dict.

Surfaces every populated EXIF tag including GPSInfo and ExifIFD subblocks,
plus any non-binary entries in PIL's ``info`` dict (PNG iTXt chunks, etc.).
This is what Windows 11 reads to populate Photo properties (Date taken,
Camera maker, Camera model, F-stop, ExposureTime, ISO, GPS, ...).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from automafile.extractors._meta import collect
from automafile.extractors.base import CorruptDocumentError, ExtractedDoc


# EXIF sub-IFD tag IDs (PIL exposes these via Image.Exif.get_ifd)
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

    # ExifIFD subblock — date taken, exposure, ISO, lens, etc.
    try:
        sub = exif.get_ifd(_EXIF_IFD_TAG)
    except Exception:
        sub = None
    if sub:
        for tag_id, value in sub.items():
            name = ExifTags.TAGS.get(tag_id, f"tag_{tag_id}")
            out.setdefault(name, value)

    # GPS subblock — coordinates, altitude, etc.
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

    metadata: dict[str, Any] = {}
    try:
        with Image.open(path) as im:
            # PIL info dict — PNG iTXt chunks (Title, Description, ...),
            # JPEG comments, etc. Binary blobs (icc_profile, exif, XMP) are
            # filtered out by ``collect``'s skip-list and value rules.
            info = getattr(im, "info", {}) or {}
            metadata.update(collect(info, prefix="info_"))
            # Image basics — Windows 11 surfaces dimensions and color mode.
            try:
                metadata["dimensions"] = f"{im.width}x{im.height}"
                metadata["color_mode"] = im.mode
                if getattr(im, "format", None):
                    metadata["pil_format"] = im.format
            except Exception:
                pass
            # EXIF (incl. GPS + ExifIFD).
            metadata.update(collect(_read_exif(im), prefix="exif_"))
    except Exception:
        pass

    return ExtractedDoc(
        path=path,
        text="",
        format="image",
        ocr_decision="ocr_full",
        extracted_metadata=metadata,
    )
