"""OCR bookkeeping shared by extractors that iterate pages or frames."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class OcrTracker:
    """Tracks per-page OCR outcomes and derives a final ``ocr_decision`` label."""

    pages: list[int] = field(default_factory=list)
    unavailable: bool = False
    failed: bool = False

    def decision(self, *, success: str = "ocr_pages") -> str:
        if self.pages:
            return success
        if self.failed:
            return "ocr_failed"
        if self.unavailable:
            return "ocr_unavailable"
        return "no_ocr"
