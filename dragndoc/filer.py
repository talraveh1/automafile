"""File-move + DB-row update for the /triage skill."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from dragndoc.config import get_settings
from dragndoc.db import transaction
from dragndoc.log import get_logger
from dragndoc.meta_store import get_by_path, relative_to_root
from dragndoc.metadata.hashing import hash_file


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

    def category_path(self) -> str:
        """The slash-separated category form: ``Cat/Sub`` or just ``Cat``."""
        cat = (self.category or "Unknown").strip() or "Unknown"
        sub = (self.subcategory or "").strip()
        return f"{cat}/{sub}" if sub else cat


_SAFE_NAME_RE = re.compile(r'[\\/:*?"<>|]')


def slugify(value: str) -> str:
    """Conservative filename sanitizer; keeps Hebrew, strips disallowed Windows chars."""
    cleaned = _SAFE_NAME_RE.sub(" ", value)
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
    base = settings.docs / slugify(proposal.category)
    if proposal.subcategory:
        base = base / slugify(proposal.subcategory)
    return base / proposal.smart_name


def apply_filing(path: Path, proposal: FilingProposal, *, overwrite: bool = False) -> Path:
    settings = get_settings()
    if not path.exists():
        raise FileNotFoundError(path)

    target = target_path_for(proposal)
    target.parent.mkdir(parents=True, exist_ok=True)
    log.info("Filing %s -> %s", path, target)

    if target.exists() and target.resolve() != path.resolve():
        existing_hash = hash_file(target)
        new_hash = hash_file(path)
        if existing_hash == new_hash:
            log.info("Idempotent re-file: target hash matches; removing source %s", path)
            path.unlink()
            _post_move_metadata(path, target, proposal)
            return target
        if not overwrite:
            log.warning("Collision: %s exists with different content (overwrite=False)", target)
            raise TargetCollision(f"{target} already exists with different content")
        log.warning("Overwriting %s with %s", target, path)

    shutil.move(str(path), str(target))
    _post_move_metadata(path, target, proposal)
    log.info("Filed: %s", target)
    return target


def _post_move_metadata(old: Path, new: Path, proposal: FilingProposal) -> None:
    """Update the row's path + category to reflect the move.

    Handles three cases:
    - Source row exists, target row doesn't: rename + categorize.
    - Both exist (idempotent re-file or overwrite): drop the target row
      (it referred to the displaced file) then rename source.
    - Only target row exists (rare; source had no row): just refresh
      the target row's category.
    """
    old_rel = relative_to_root(old)
    new_rel = relative_to_root(new)
    with transaction() as conn:
        source = conn.execute("SELECT id FROM docs WHERE path = ?", (old_rel,)).fetchone()
        if source is not None:
            if old_rel != new_rel:
                conn.execute("DELETE FROM docs WHERE path = ?", (new_rel,))
            conn.execute(
                "UPDATE docs SET path = ?, category = ? WHERE id = ?",
                (new_rel, proposal.category_path(), source["id"]),
            )
        else:
            conn.execute(
                "UPDATE docs SET category = ? WHERE path = ?",
                (proposal.category_path(), new_rel),
            )
