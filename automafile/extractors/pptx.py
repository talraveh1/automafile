"""PPTX extractor."""

from __future__ import annotations

from pathlib import Path

from automafile.extractors.base import CorruptDocumentError, ExtractedDoc


def extract(path: Path) -> ExtractedDoc:
    try:
        from pptx import Presentation
    except ImportError as exc:
        raise CorruptDocumentError("python-pptx is not installed") from exc
    try:
        pres = Presentation(str(path))
    except Exception as exc:  # noqa: BLE001
        raise CorruptDocumentError(f"pptx failed for {path}: {exc}") from exc

    chunks: list[str] = []
    for slide_idx, slide in enumerate(pres.slides, start=1):
        chunks.append(f"# Slide {slide_idx}")
        for shape in slide.shapes:
            if shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    line = "".join(run.text for run in para.runs)
                    if line.strip():
                        chunks.append(line)

    metadata: dict = {}
    try:
        cp = pres.core_properties
        for attr in ("title", "subject", "keywords", "category", "comments", "author"):
            v = getattr(cp, attr, None)
            if v:
                metadata[attr] = v
    except Exception:
        pass

    return ExtractedDoc(
        path=path,
        text="\n".join(chunks),
        native_metadata=metadata,
        format="pptx",
        supports_native_metadata=True,
    )
