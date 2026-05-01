"""Image extractor: text comes from OCR; native metadata via Pillow EXIF/XMP."""

from __future__ import annotations

from pathlib import Path

from automafile.extractors.base import CorruptDocumentError, ExtractedDoc


def _read_image_metadata(path: Path) -> dict:
    metadata: dict = {}
    try:
        from PIL import Image, ExifTags  # noqa: F401

        try:
            import pillow_heif  # noqa: F401
            pillow_heif.register_heif_opener()
        except Exception:
            pass

        with Image.open(path) as im:
            exif = im.getexif() if hasattr(im, "getexif") else None
            if exif:
                for tag_id, value in exif.items():
                    name = ExifTags.TAGS.get(tag_id, str(tag_id))
                    metadata[f"exif_{name}"] = str(value)[:500]
            info = getattr(im, "info", {}) or {}
            for k, v in info.items():
                if isinstance(v, (str, bytes)):
                    metadata[f"info_{k}"] = v if isinstance(v, str) else v.decode("utf-8", "replace")
    except Exception:
        pass
    return metadata


def extract(path: Path) -> ExtractedDoc:
    try:
        from PIL import Image  # noqa: F401
    except ImportError as exc:
        raise CorruptDocumentError("Pillow is not installed") from exc

    metadata = _read_image_metadata(path)
    return ExtractedDoc(
        path=path,
        text="",
        native_metadata=metadata,
        format="image",
        supports_native_metadata=True,
        ocr_decision="ocr_full",
    )
