"""Pydantic models for sidecar metadata."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


SCHEMA_VERSION = 1


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class OcrBlock(BaseModel):
    decision: str = "never"
    done_at: str | None = None
    engine: str | None = None
    engine_version: str | None = None
    languages: str | None = None


class MetadataDoc(BaseModel):
    """The structured fields stored in the sidecar frontmatter."""

    model_config = ConfigDict(extra="allow")

    schema_version: int = SCHEMA_VERSION
    content_hash: str
    file_size: int
    filename_at_creation: str
    relative_path: str
    language: str = "unknown"
    tags: list[str] = Field(default_factory=list)
    category: str = "Unknown"
    subcategory: str | None = None
    correspondent: str | None = None
    date: str | None = None
    amount: float | None = None
    currency: str | None = None
    title: str | None = None
    confidence: str = "low"
    needs_review: bool = True
    ocr: OcrBlock = Field(default_factory=OcrBlock)
    metadata_modified: str = Field(default_factory=utc_now_iso)
    metadata_modified_by: str = "automafile-watcher 0.1.0"
    filed_at: str | None = None
    filed_path: str | None = None
    summary: str = ""

    def to_frontmatter_dict(self) -> dict[str, Any]:
        d = self.model_dump(exclude={"summary"})
        d["ocr"] = self.ocr.model_dump()
        return d
