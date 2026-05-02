"""Tree walker; emits a worklist describing what needs OCR / metadata / review."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from automafile.config import get_settings
from automafile.log import get_logger
from automafile.metadata.hashing import hash_file
from automafile.metadata.reconcile import find_orphans
from automafile.metadata.sidecar import read as sidecar_read, sidecar_path_for
from automafile.ocr import (
    pdf_ocr_decision,
    tesseract_languages,
    tesseract_version,
)


log = get_logger(__name__)


SUPPORTED_EXT = {
    ".pdf", ".docx", ".xlsx", ".pptx",
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp", ".heic", ".heif",
    ".html", ".htm", ".epub", ".txt", ".md", ".markdown", ".csv", ".log", ".json",
    ".xml", ".yaml", ".yml",
}

OCR_REVIEW_GRACE_DAYS = 0


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
    orphan_sidecars: list[dict] = field(default_factory=list)
    quarantined_sidecars: list[dict] = field(default_factory=list)
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
            "orphan_sidecars": self.orphan_sidecars,
            "quarantined_sidecars": self.quarantined_sidecars,
            "unprocessable_files": self.unprocessable_files,
        }


def _rel(path: Path) -> str:
    settings = get_settings()
    try:
        return str(path.relative_to(settings.documents_root)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


REQUIRED_METADATA_FIELDS = ("category", "summary", "tags")


def _metadata_completeness(doc, summary_body: str | None) -> list[str]:
    missing: list[str] = []
    if not (doc.category and doc.category != "Unknown"):
        missing.append("category")
    if not (summary_body or doc.title):
        missing.append("summary")
    if not doc.tags:
        missing.append("tags")
    return missing


def _is_stale(doc, file_path: Path) -> tuple[bool, int]:
    try:
        file_mt = datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc)
    except FileNotFoundError:
        return False, 0
    try:
        meta_mt = datetime.fromisoformat(doc.metadata_modified.replace("Z", "+00:00"))
    except Exception:
        return False, 0
    if file_mt <= meta_mt:
        return False, 0
    delta_days = (file_mt - meta_mt).days
    return delta_days > 0, delta_days


def _ocr_config_drift(doc, current_engine: str, current_langs: str) -> bool:
    prev_engine = doc.ocr.engine_version or ""
    prev_langs = doc.ocr.languages or ""
    if not (prev_engine or prev_langs):
        return False
    if not doc.ocr.done_at:
        return False
    return (prev_engine != current_engine) or (prev_langs != current_langs)


def run_scan(documents_root: Path | None = None) -> Worklist:
    settings = get_settings()
    root = documents_root or settings.documents_root
    if not root.exists():
        root.mkdir(parents=True, exist_ok=True)

    wl = Worklist(ran_at=_utc_now_iso(), documents_root=str(root))
    current_engine = tesseract_version()
    current_langs = settings.tesseract_langs

    for path in root.rglob("*"):
        if not path.is_file():
            continue
        # any dot-prefixed path component (covers .meta/ and any other hidden dirs)
        rel_parts = path.relative_to(root).parts
        if any(p.startswith(".") for p in rel_parts):
            wl.skipped += 1
            continue
        ext = path.suffix.lower()
        wl.files_seen += 1
        wl.tree_size += 1

        if ext not in SUPPORTED_EXT:
            wl.skipped += 1
            continue

        # OCR-needed?
        if ext == ".pdf":
            try:
                decision = pdf_ocr_decision(path)
                if decision.action == "skip_encrypted":
                    wl.unprocessable_files.append({
                        "relative_path": _rel(path),
                        "reason": "pdf_encrypted",
                    })
                    continue
                if decision.action in {"ocr_full", "ocr_pages"}:
                    if not _has_metadata(path):
                        wl.files_needing_ocr.append({
                            "relative_path": _rel(path),
                            "reason": decision.reason or decision.action,
                        })
            except Exception as exc:  # noqa: BLE001
                wl.unprocessable_files.append({
                    "relative_path": _rel(path),
                    "reason": f"pdf_check_failed: {exc}",
                })
                continue
        elif ext in {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".heic", ".heif", ".bmp", ".gif"}:
            if not _has_metadata(path):
                wl.files_needing_ocr.append({
                    "relative_path": _rel(path),
                    "reason": "image_format",
                })

        # metadata?
        doc, summary_body, _ = sidecar_read(path)
        if doc is None and not _has_native_metadata(path):
            wl.files_needing_metadata.append({
                "relative_path": _rel(path),
                "format": ext.lstrip("."),
                "reason": "no_metadata_present",
            })
            continue
        if doc is not None:
            missing = _metadata_completeness(doc, summary_body)
            if missing:
                wl.files_with_partial_metadata.append({
                    "relative_path": _rel(path),
                    "missing_fields": missing,
                })
            stale, delta_days = _is_stale(doc, path)
            if stale:
                wl.files_with_stale_metadata.append({
                    "relative_path": _rel(path),
                    "metadata_modified": doc.metadata_modified,
                    "file_modified": _utc_now_iso(),
                    "delta_days": delta_days,
                })
            if _ocr_config_drift(doc, current_engine, current_langs):
                wl.ocr_review_candidates.append({
                    "relative_path": _rel(path),
                    "previous_engine": doc.ocr.engine_version,
                    "previous_languages": doc.ocr.languages,
                    "current_engine": current_engine,
                    "current_languages": current_langs,
                })

    # orphans
    for orphan in find_orphans(root):
        wl.orphan_sidecars.append({
            "sidecar_relative_path": str(orphan.sidecar_path.relative_to(root)).replace("\\", "/"),
            "missing_path": orphan.described_relative_path,
            "hash_in_sidecar": orphan.sidecar_hash,
            "matches_in_tree": [_rel(p) for p in orphan.matches_in_tree],
        })

    # quarantined sidecars (corrupt files moved aside by sidecar.read)
    meta_name = settings.meta_subfolder
    for path in root.rglob(f"{meta_name}/*.broken-*"):
        if not path.is_file():
            continue
        # the original sidecar name had ``.broken-<ts>`` appended; strip that
        # to recover the filename it described
        original_sidecar_name = re.sub(r"\.broken-\d{8}-\d{6}$", "", path.name)
        described_filename = original_sidecar_name[:-3] if original_sidecar_name.endswith(".md") else original_sidecar_name
        described_path = path.parent.parent / described_filename
        wl.quarantined_sidecars.append({
            "quarantine_relative_path": str(path.relative_to(root)).replace("\\", "/"),
            "for_file": _rel(described_path),
            "original_filename": original_sidecar_name,
        })

    return wl


def _has_metadata(path: Path) -> bool:
    if sidecar_path_for(path).exists():
        return True
    return _has_native_metadata(path)


def _has_native_metadata(path: Path) -> bool:
    try:
        from automafile.metadata.native import read_native, supports
    except ImportError:
        return False
    if not supports(path):
        return False
    try:
        meta = read_native(path)
        # consider "has native" if at least one of these keys is present and non-empty
        for k in ("Title", "Subject", "Keywords", "title", "subject", "keywords",
                  "dc:title", "dc:description", "dc:subject"):
            if meta.get(k):
                return True
    except Exception:
        return False
    return False


def write_worklist(wl: Worklist) -> Path:
    settings = get_settings()
    settings.scan_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out = settings.scan_dir / f"scan-{ts}.json"
    out.write_text(json.dumps(wl.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    return out
