"""File-move + sidecar-move + metadata-update for the /triage skill."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from dragndoc.config import get_settings
from dragndoc.log import get_logger
from dragndoc.metadata.hashing import hash_file
from dragndoc.metadata.schema import utc_now_iso
from dragndoc.metadata.sidecar import (
    read as sidecar_read,
    sidecar_path_for,
    update_relative_path,
    write as sidecar_write,
)


log = get_logger(__name__)


class TargetCollision(Exception):
    """Raised when the destination exists with a different hash."""


@dataclass
class FilingProposal:
    category: str
    subcategory: str | None
    smart_name: str

    def to_dict(self) -> dict:
        return {
            "category": self.category,
            "subcategory": self.subcategory,
            "smart_name": self.smart_name,
        }


_SAFE_NAME_RE = re.compile(r'[\\/:*?"<>|]')


def slugify(value: str) -> str:
    """Conservative filename sanitizer; keeps Hebrew, strips disallowed Windows chars."""
    if value is None:
        return ""
    cleaned = _SAFE_NAME_RE.sub(" ", str(value))
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:120]


def smart_filename(meta: dict, ext: str) -> str:
    """Compose ``{date} - {correspondent} - {topic}.{ext}`` from a meta dict."""
    parts: list[str] = []
    date = meta.get("date")
    if date:
        parts.append(slugify(str(date)))
    correspondent = meta.get("correspondent") or meta.get("Correspondent")
    if correspondent:
        parts.append(slugify(str(correspondent)))
    topic = meta.get("title") or meta.get("Title")
    if not topic:
        summary = meta.get("summary") or ""
        topic = " ".join(summary.split()[:6]) if summary else "untitled"
    parts.append(slugify(str(topic)))
    base = " - ".join(p for p in parts if p) or "untitled"
    return f"{base}.{ext.lstrip('.')}"


def propose_filing(meta: dict, *, default_category: str = "Unknown") -> FilingProposal:
    category = meta.get("category") or default_category
    subcategory = meta.get("subcategory")
    ext = meta.get("extension") or ""
    return FilingProposal(
        category=str(category),
        subcategory=str(subcategory) if subcategory else None,
        smart_name=smart_filename(meta, ext),
    )


def target_path_for(proposal: FilingProposal) -> Path:
    settings = get_settings()
    base = settings.documents_root / slugify(proposal.category)
    if proposal.subcategory:
        base = base / slugify(proposal.subcategory)
    return base / proposal.smart_name


def apply_filing(path: Path, proposal: FilingProposal, *, overwrite: bool = False) -> Path:
    settings = get_settings()
    if not path.exists():
        raise FileNotFoundError(path)

    target = target_path_for(proposal)
    target.parent.mkdir(parents=True, exist_ok=True)
    log.info("filing %s -> %s", path, target)

    if target.exists() and target.resolve() != path.resolve():
        # the outer guard already established the two paths differ; if hashes match
        # we treat it as an idempotent re-file, otherwise it's a true collision
        existing_hash = hash_file(target)
        new_hash = hash_file(path)
        if existing_hash == new_hash:
            log.info("idempotent re-file: target hash matches; removing source %s", path)
            path.unlink()
            _post_move_metadata(target, proposal)
            return target
        if not overwrite:
            log.warning("collision: %s exists with different content (overwrite=False)", target)
            raise TargetCollision(f"{target} already exists with different content")
        log.warning("overwriting %s with %s", target, path)

    shutil.move(str(path), str(target))
    sidecar_old = sidecar_path_for(path)
    if sidecar_old.exists():
        update_relative_path(path, target)

    _post_move_metadata(target, proposal)
    log.info("filed: %s", target)
    return target


def _post_move_metadata(target: Path, proposal: FilingProposal) -> None:
    settings = get_settings()
    doc, summary, notes = sidecar_read(target)
    if doc is None:
        return
    try:
        rel = str(target.relative_to(settings.documents_root)).replace("\\", "/")
    except ValueError:
        rel = str(target).replace("\\", "/")
    doc.relative_path = rel
    doc.category = proposal.category
    if proposal.subcategory:
        doc.subcategory = proposal.subcategory
    doc.filed_at = utc_now_iso()
    doc.filed_path = rel
    doc.metadata_modified = utc_now_iso()
    sidecar_write(target, doc, summary, notes)
