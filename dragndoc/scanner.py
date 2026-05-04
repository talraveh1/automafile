"""Tree walker; emits an in-memory worklist describing what needs OCR / metadata / review.

The scanner no longer writes JSON to disk. Callers (``dnd process``,
``dnd review``) consume the returned :class:`Worklist` directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dragndoc.config import get_settings
from dragndoc.db import connect
from dragndoc.log import get_logger
from dragndoc.meta_store import relative_to_root
from dragndoc.metadata.reconcile import OrphanReport, find_orphans
from dragndoc.ocr import (
    pdf_ocr_decision,
    tesseract_languages,
    tesseract_version,
)
from dragndoc.treewalk import iter_unblocked_files


log = get_logger(__name__)


SUPPORTED_EXT = {
    ".pdf", ".docx", ".xlsx", ".pptx",
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp", ".heic", ".heif",
    ".html", ".htm", ".epub", ".txt", ".md", ".markdown", ".csv", ".log", ".json",
    ".xml", ".yaml", ".yml",
}


REQUIRED_METADATA_FIELDS = ("category", "summary", "tags")


@dataclass
class Worklist:
    ran_at: str
    documents_root: str
    tree_size: int = 0
    files_seen: int = 0
    skipped: int = 0
    files_needing_ocr: list[dict] = field(default_factory=list)
    files_needing_metadata: list[dict] = field(default_factory=list)
    files_with_partial_metadata: list[dict] = field(default_factory=list)
    files_with_stale_metadata: list[dict] = field(default_factory=list)
    ocr_review_candidates: list[dict] = field(default_factory=list)
    missing_files: list[dict] = field(default_factory=list)
    unprocessable_files: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ran_at": self.ran_at,
            "documents_root": self.documents_root,
            "tree_size": self.tree_size,
            "files_seen": self.files_seen,
            "skipped": self.skipped,
            "files_needing_ocr": self.files_needing_ocr,
            "files_needing_metadata": self.files_needing_metadata,
            "files_with_partial_metadata": self.files_with_partial_metadata,
            "files_with_stale_metadata": self.files_with_stale_metadata,
            "ocr_review_candidates": self.ocr_review_candidates,
            "missing_files": self.missing_files,
            "unprocessable_files": self.unprocessable_files,
        }


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _index_existing_rows() -> dict[str, dict[str, Any]]:
    """Snapshot every ``docs`` row keyed by ``path``. Read-only, single SELECT."""
    by_path: dict[str, dict[str, Any]] = {}
    with connect(readonly=True) as conn:
        rows = conn.execute(
            "SELECT id, path, hash, size, modified, digested, category, "
            "title, tags, summary, "
            "(SELECT engine_ver FROM ocr WHERE doc_id = docs.id) AS ocr_engine_ver, "
            "(SELECT langs FROM ocr WHERE doc_id = docs.id) AS ocr_langs, "
            "(SELECT done FROM ocr WHERE doc_id = docs.id) AS ocr_done "
            "FROM docs"
        ).fetchall()
    for r in rows:
        by_path[r["path"]] = dict(r)
    return by_path


def _is_partial(row: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    if not row.get("category") or row["category"] == "Unknown":
        missing.append("category")
    if not (row.get("summary") or row.get("title")):
        missing.append("summary")
    if not row.get("tags"):
        missing.append("tags")
    return missing


def _is_stale(row: dict[str, Any], file_path: Path) -> tuple[bool, int]:
    try:
        file_mt = datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc)
    except FileNotFoundError:
        return False, 0
    modified = row.get("modified")
    if not modified:
        return False, 0
    try:
        record_mt = datetime.fromisoformat(str(modified).replace("Z", "+00:00"))
    except ValueError:
        return False, 0
    if file_mt <= record_mt:
        return False, 0
    return True, (file_mt - record_mt).days


def _ocr_drift(row: dict[str, Any], current_engine: str, current_langs: str) -> bool:
    prev_engine = row.get("ocr_engine_ver") or ""
    prev_langs = row.get("ocr_langs") or ""
    done = row.get("ocr_done") or ""
    if not done:
        return False
    if not (prev_engine or prev_langs):
        return False
    # row's ocr_langs is in semilist form (";heb;eng;"); the current value
    # comes from settings as "heb+eng". Normalize both to a sorted set for
    # comparison.
    return _normalize_langs(prev_langs) != _normalize_langs(current_langs) or prev_engine != current_engine


def _normalize_langs(value: str) -> tuple[str, ...]:
    if not value:
        return ()
    parts: list[str] = []
    for chunk in value.replace("+", ";").split(";"):
        chunk = chunk.strip()
        if chunk:
            parts.append(chunk)
    return tuple(sorted(set(parts)))


def run_scan(documents_root: Path | None = None, subpath: Path | None = None) -> Worklist:
    settings = get_settings()
    root = documents_root or settings.documents_root
    if not root.exists():
        root.mkdir(parents=True, exist_ok=True)

    if subpath is not None:
        if subpath.is_absolute():
            raise ValueError(f"subpath must be relative: {subpath}")
        walk_root = (root / subpath).resolve()
        if not walk_root.is_relative_to(root.resolve()):
            raise ValueError(f"subpath escapes documents_root: {subpath}")
        if not walk_root.exists():
            raise FileNotFoundError(f"subpath does not exist: {walk_root}")
    else:
        walk_root = root

    log.info("scan starting under %s", walk_root)
    wl = Worklist(ran_at=_utc_now_iso(), documents_root=str(root))
    current_engine = tesseract_version()
    current_langs = settings.tesseract_langs
    rows_by_path = _index_existing_rows()

    current_directory: Path | None = None
    seen_paths: set[str] = set()

    for path in iter_unblocked_files(walk_root):
        if path.parent != current_directory:
            current_directory = path.parent
            log.info("scan: entering %s", current_directory)

        ext = path.suffix.lower()
        wl.files_seen += 1
        wl.tree_size += 1
        rel = relative_to_root(path)
        seen_paths.add(rel)

        if ext not in SUPPORTED_EXT:
            wl.skipped += 1
            continue

        row = rows_by_path.get(rel)

        # OCR-needed?
        if ext == ".pdf":
            try:
                decision = pdf_ocr_decision(path)
                if decision.action == "skip_encrypted":
                    wl.unprocessable_files.append({
                        "relative_path": rel,
                        "reason": "pdf_encrypted",
                    })
                    continue
                if decision.action in {"ocr_full", "ocr_pages"} and row is None:
                    wl.files_needing_ocr.append({
                        "relative_path": rel,
                        "reason": decision.reason or decision.action,
                    })
            except Exception as exc:  # noqa: BLE001
                wl.unprocessable_files.append({
                    "relative_path": rel,
                    "reason": f"pdf_check_failed: {exc}",
                })
                continue
        elif ext in {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".heic", ".heif", ".bmp", ".gif"}:
            if row is None:
                wl.files_needing_ocr.append({
                    "relative_path": rel,
                    "reason": "image_format",
                })

        # Metadata?
        if row is None:
            wl.files_needing_metadata.append({
                "relative_path": rel,
                "format": ext.lstrip("."),
                "reason": "no_record",
            })
            continue

        partial = _is_partial(row)
        if partial:
            wl.files_with_partial_metadata.append({
                "relative_path": rel,
                "missing_fields": partial,
            })
        stale, delta_days = _is_stale(row, path)
        if stale:
            wl.files_with_stale_metadata.append({
                "relative_path": rel,
                "metadata_modified": row.get("modified"),
                "file_modified": _utc_now_iso(),
                "delta_days": delta_days,
            })
        if _ocr_drift(row, current_engine, current_langs):
            wl.ocr_review_candidates.append({
                "relative_path": rel,
                "previous_engine": row.get("ocr_engine_ver"),
                "previous_languages": row.get("ocr_langs"),
                "current_engine": current_engine,
                "current_languages": current_langs,
            })

    # Missing files: rows whose ``path`` doesn't resolve to a file on disk.
    # We restrict to rows under ``walk_root`` so a sub-path scan only reports
    # missing entries within that sub-path.
    walk_rel_prefix = ""
    if subpath is not None:
        try:
            walk_rel_prefix = str(walk_root.relative_to(root.resolve())).replace("\\", "/").rstrip("/") + "/"
        except ValueError:
            walk_rel_prefix = ""

    for rel, row in rows_by_path.items():
        if walk_rel_prefix and not rel.startswith(walk_rel_prefix):
            continue
        if rel in seen_paths:
            continue
        # The row exists but the file wasn't seen during the walk: either
        # truly missing, or living in a blocked subtree. Treat it as missing
        # for review purposes.
        full = root / rel
        if full.exists():
            continue
        wl.missing_files.append({
            "relative_path": rel,
            "doc_id": row["id"],
            "hash": row.get("hash"),
            "size": row.get("size"),
        })

    log.info(
        "scan complete under %s: seen=%d skipped=%d need_ocr=%d need_meta=%d "
        "partial=%d stale=%d ocr_review=%d missing=%d unprocessable=%d",
        walk_root, wl.files_seen, wl.skipped,
        len(wl.files_needing_ocr), len(wl.files_needing_metadata),
        len(wl.files_with_partial_metadata), len(wl.files_with_stale_metadata),
        len(wl.ocr_review_candidates), len(wl.missing_files),
        len(wl.unprocessable_files),
    )
    return wl
