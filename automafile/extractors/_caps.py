"""Adaptive extraction caps shared by sectioned extractors."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from automafile.config import Settings


@dataclass(frozen=True)
class CapConfig:
    min_pages: int = 3
    max_pages: int = 5
    per_page_chars: int = 1500
    target_chars: int = 6000

    @classmethod
    def from_settings(cls, settings: "Settings") -> "CapConfig":
        extraction = settings.extraction
        return cls(
            min_pages=extraction.min_pages,
            max_pages=extraction.max_pages,
            per_page_chars=extraction.per_page_chars,
            target_chars=extraction.target_chars,
        )


def trim_to_word_boundary(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    last_ws = max(cut.rfind(" "), cut.rfind("\n"), cut.rfind("\t"))
    if last_ws >= 0 and last_ws >= max_chars - 50:
        return cut[:last_ws].rstrip()
    return cut.rstrip()


def select_pages(page_texts: Iterable[str], cfg: CapConfig) -> list[str]:
    """Select a bounded prefix of pages, trimming each page on a word boundary."""
    kept: list[str] = []
    iterator = iter(page_texts)
    for _ in range(cfg.max_pages):
        try:
            raw = next(iterator)
        except StopIteration:
            break
        trimmed = trim_to_word_boundary(raw, cfg.per_page_chars)
        kept.append(trimmed)
        if len(kept) >= cfg.min_pages and sum(len(page) for page in kept) >= cfg.target_chars:
            break
    return kept
