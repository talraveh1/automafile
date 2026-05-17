"""ASR bookkeeping shared by extractors that rescue audio/video files."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AsrTracker:
    """Tracks one ASR pass and derives a final ``asr_decision`` label.

    Mirrors :class:`dragndoc.extractors._ocr.OcrTracker` but for audio/video.
    """

    ran: bool = False
    unavailable: bool = False
    failed: bool = False
    used_subtitle: bool = False

    def decision(self, *, success: str = "asr_full") -> str:
        if self.used_subtitle:
            return "asr_subtitle"
        if self.ran:
            return success
        if self.failed:
            return "asr_failed"
        if self.unavailable:
            return "asr_unavailable"
        return "no_asr"
