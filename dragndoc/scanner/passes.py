"""Multi-pass tree scanner and reconciler."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from dragndoc.config import get_settings
from dragndoc.db import connect, transaction
from dragndoc.dirs import observe_tree
from dragndoc.log import get_logger
from dragndoc.meta_store import (
    _recompute_dups_for_hashes,
    file_modified_iso,
    relative_to_root,
    utc_now_iso,
)
from dragndoc.metadata.hashing import hash_file
from dragndoc.ocr import pdf_ocr_decision
from dragndoc.scanner.reconcile import resolve_path_conflict
from dragndoc.scanner.worklist import (
    DigestCandidate,
    MergeRecord,
    OrphanInfo,
    ReconciliationReport,
    ScanReport,
    UnprocessableEntry,
    WorklistForDigest,
)
from dragndoc.treewalk import iter_unblocked_files


log = get_logger("dragndoc.scanner")


SUPPORTED_EXT = {
    ".pdf", ".docx", ".xlsx", ".pptx",
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp", ".heic", ".heif",
    ".html", ".htm", ".epub", ".txt", ".md", ".markdown", ".csv", ".log", ".json",
    ".xml", ".yaml", ".yml",
}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".heic", ".heif", ".bmp", ".gif"}


@dataclass
class FileFacts:
    rel: str
    path: Path
    size: int
    mtime: str | None
    ext: str
    details: dict[str, Any]


@dataclass
class RenamePlan:
    old_rel: str
    new_rel: str
    old_row: sqlite3.Row
    new_row: sqlite3.Row | None
    file_hash: str


def _index_existing_rows() -> dict[str, sqlite3.Row]:
    with connect(readonly=True) as conn:
        rows = conn.execute(
            "SELECT id, path, hash, size, modified, digested, category, summary, tags, "
            "title, notes, parties, langs, date, confidence, dup, extra, "
            "ocr_decision, ocr_done, ocr_engine, ocr_engine_ver, ocr_langs "
            "FROM docs_full"
        ).fetchall()
    return {row["path"]: row for row in rows}


def _is_partial(row: sqlite3.Row) -> list[str]:
    missing: list[str] = []
    if not row["category"] or row["category"] == "Unknown":
        missing.append("category")
    if not (row["summary"] or row["title"]):
        missing.append("summary")
    if not row["tags"]:
        missing.append("tags")
    return missing


def _is_stale(row: sqlite3.Row, facts: FileFacts) -> tuple[bool, int]:
    if not row["modified"] or not facts.mtime:
        return False, 0
    try:
        file_mt = datetime.fromisoformat(facts.mtime.replace("Z", "+00:00"))
        record_mt = datetime.fromisoformat(str(row["modified"]).replace("Z", "+00:00"))
    except ValueError:
        return False, 0
    if file_mt <= record_mt:
        return False, 0
    return True, (file_mt - record_mt).days


def _normalize_langs(value: str) -> tuple[str, ...]:
    if not value:
        return ()
    parts: list[str] = []
    for chunk in value.replace("+", ";").split(";"):
        chunk = chunk.strip()
        if chunk:
            parts.append(chunk)
    return tuple(sorted(set(parts)))


def _current_tesseract_version() -> str:
    from dragndoc import scanner as scanner_pkg

    return scanner_pkg.tesseract_version()


def _ocr_drift(row: sqlite3.Row, current_engine: str, current_langs: str) -> bool:
    prev_engine = row["ocr_engine_ver"] or ""
    prev_langs = row["ocr_langs"] or ""
    done = row["ocr_done"] or ""
    if not done:
        return False
    if not (prev_engine or prev_langs):
        return False
    return _normalize_langs(prev_langs) != _normalize_langs(current_langs) or prev_engine != current_engine


def _walk_prefix(root: Path, walk_root: Path, subpath: Path | None) -> str:
    if subpath is None:
        return ""
    try:
        return str(walk_root.relative_to(root.resolve())).replace("\\", "/").rstrip("/") + "/"
    except ValueError:
        return ""


def _resolve_walk_root(root: Path, subpath: Path | None) -> Path:
    if subpath is None:
        return root
    if subpath.is_absolute():
        raise ValueError(f"subpath must be relative: {subpath}")
    walk_root = (root / subpath).resolve()
    if not walk_root.is_relative_to(root.resolve()):
        raise ValueError(f"subpath escapes docs root: {subpath}")
    if not walk_root.exists():
        raise FileNotFoundError(f"subpath does not exist: {walk_root}")
    return walk_root


def _inventory(
    root: Path,
    walk_root: Path,
    rows_by_path: dict[str, sqlite3.Row],
) -> tuple[dict[str, FileFacts], int, int, list[UnprocessableEntry]]:
    fs_facts: dict[str, FileFacts] = {}
    files_seen = 0
    skipped = 0
    unprocessable: list[UnprocessableEntry] = []
    current_directory: Path | None = None
    for path in iter_unblocked_files(walk_root):
        if path.parent != current_directory:
            current_directory = path.parent
            log.info("scan: entering %s", current_directory)
        files_seen += 1
        ext = path.suffix.lower()
        if ext not in SUPPORTED_EXT:
            skipped += 1
            continue
        rel = relative_to_root(path)
        details: dict[str, Any] = {"format": ext.lstrip(".")}
        if ext == ".pdf":
            try:
                decision = pdf_ocr_decision(path)
            except Exception as exc:  # noqa: BLE001
                unprocessable.append(UnprocessableEntry(rel=rel, reason=f"pdf_check_failed: {exc}"))
                continue
            if decision.action == "skip_encrypted":
                unprocessable.append(UnprocessableEntry(rel=rel, reason="pdf_encrypted"))
                continue
            if decision.action in {"ocr_full", "ocr_pages"} and rel not in rows_by_path:
                details["needs_ocr"] = True
                details["ocr_reason"] = decision.reason or decision.action
        elif ext in IMAGE_EXT and rel not in rows_by_path:
            details["needs_ocr"] = True
            details["ocr_reason"] = "image_format"
        try:
            st = path.stat()
        except OSError as exc:
            unprocessable.append(UnprocessableEntry(rel=rel, reason=f"stat_failed: {exc}"))
            continue
        fs_facts[rel] = FileFacts(
            rel=rel,
            path=path,
            size=st.st_size,
            mtime=file_modified_iso(path),
            ext=ext,
            details=details,
        )
    return fs_facts, files_seen, skipped, unprocessable


def _row_facts_match(row: sqlite3.Row, facts: FileFacts) -> bool:
    return row["size"] == facts.size and row["modified"] == facts.mtime


def _hash_candidate(
    rel: str,
    facts: FileFacts,
    rows_by_path: dict[str, sqlite3.Row],
    fresh_hashes: dict[str, str],
) -> str | None:
    if rel in fresh_hashes:
        return fresh_hashes[rel]
    row = rows_by_path.get(rel)
    if row is not None and _row_facts_match(row, facts):
        return row["hash"]
    try:
        fresh_hashes[rel] = hash_file(facts.path)
    except OSError:
        return None
    return fresh_hashes[rel]


def _plan_known_hash_renames(
    *,
    fs_facts: dict[str, FileFacts],
    rows_by_path: dict[str, sqlite3.Row],
    db_only: set[str],
    fs_only: set[str],
    both: set[str],
    fresh_hashes: dict[str, str],
) -> tuple[list[RenamePlan], set[str]]:
    plans: list[RenamePlan] = []
    unresolved = set(db_only)
    claimed_targets: set[str] = set()
    for old_rel in sorted(db_only):
        old_row = rows_by_path[old_rel]
        for rel in sorted((fs_only | both) - claimed_targets):
            facts = fs_facts[rel]
            if facts.size != old_row["size"]:
                continue
            candidate_hash = _hash_candidate(rel, facts, rows_by_path, fresh_hashes)
            if candidate_hash != old_row["hash"]:
                continue
            plans.append(
                RenamePlan(
                    old_rel=old_rel,
                    new_rel=rel,
                    old_row=old_row,
                    new_row=rows_by_path.get(rel),
                    file_hash=candidate_hash,
                )
            )
            unresolved.discard(old_rel)
            claimed_targets.add(rel)
            break
    return plans, unresolved


def _selective_rehash(
    *,
    fs_facts: dict[str, FileFacts],
    rows_by_path: dict[str, sqlite3.Row],
    fs_only: set[str],
    both: set[str],
    resolved_targets: set[str],
    fresh_hashes: dict[str, str],
    rehash: bool,
) -> None:
    for rel in sorted(fs_only - resolved_targets):
        if rel not in fresh_hashes:
            fresh_hashes[rel] = hash_file(fs_facts[rel].path)
    for rel in sorted(both):
        row = rows_by_path[rel]
        facts = fs_facts[rel]
        if rehash or not _row_facts_match(row, facts):
            fresh_hashes[rel] = hash_file(facts.path)


def _plan_fresh_hash_renames(
    *,
    rows_by_path: dict[str, sqlite3.Row],
    fs_facts: dict[str, FileFacts],
    unresolved: set[str],
    fresh_hashes: dict[str, str],
    claimed_targets: set[str],
) -> tuple[list[RenamePlan], set[str]]:
    plans: list[RenamePlan] = []
    still_unresolved = set(unresolved)
    for old_rel in sorted(unresolved):
        old_row = rows_by_path[old_rel]
        for rel, candidate_hash in sorted(fresh_hashes.items()):
            if rel in claimed_targets:
                continue
            if candidate_hash != old_row["hash"]:
                continue
            plans.append(
                RenamePlan(
                    old_rel=old_rel,
                    new_rel=rel,
                    old_row=old_row,
                    new_row=rows_by_path.get(rel),
                    file_hash=candidate_hash,
                )
            )
            still_unresolved.discard(old_rel)
            claimed_targets.add(rel)
            break
    return plans, still_unresolved


def _rows_in_scope(rows_by_path: dict[str, sqlite3.Row], walk_prefix: str) -> dict[str, sqlite3.Row]:
    if not walk_prefix:
        return rows_by_path
    prefix_no_slash = walk_prefix.rstrip("/")
    return {
        rel: row
        for rel, row in rows_by_path.items()
        if rel == prefix_no_slash or rel.startswith(walk_prefix)
    }


def _fetch_rows_by_path(conn: sqlite3.Connection | None = None) -> dict[str, sqlite3.Row]:
    sql = (
        "SELECT id, path, hash, size, modified, digested, category, summary, tags, "
        "title, notes, parties, langs, date, confidence, dup, extra, "
        "ocr_decision, ocr_done, ocr_engine, ocr_engine_ver, ocr_langs "
        "FROM docs_full"
    )
    if conn is not None:
        rows = conn.execute(sql).fetchall()
    else:
        with connect(readonly=True) as read_conn:
            rows = read_conn.execute(sql).fetchall()
    return {row["path"]: row for row in rows}


def _apply_reconciliation(
    *,
    plans: list[RenamePlan],
    unresolved: set[str],
    fs_facts: dict[str, FileFacts],
    fresh_hashes: dict[str, str],
    both: set[str],
    rows_by_path: dict[str, sqlite3.Row],
    apply: bool,
) -> tuple[ReconciliationReport, dict[str, sqlite3.Row]]:
    report = ReconciliationReport()
    for plan in plans:
        report.renames.append((plan.old_rel, plan.new_rel))
    if not apply:
        for rel in sorted(unresolved):
            row = rows_by_path[rel]
            report.unresolved_orphans.append(
                OrphanInfo(doc_id=row["id"], recorded_path=rel, hash=row["hash"], size=row["size"])
            )
        return report, rows_by_path

    affected_hashes: set[str] = set()
    with transaction() as conn:
        for plan in plans:
            facts = fs_facts[plan.new_rel]
            if plan.new_row is None:
                conn.execute(
                    "UPDATE docs SET path = ?, hash = ?, size = ?, modified = ? WHERE id = ?",
                    (plan.new_rel, plan.file_hash, facts.size, facts.mtime, plan.old_row["id"]),
                )
                continue
            merged = resolve_path_conflict(
                conn,
                old_row=plan.old_row,
                new_row=plan.new_row,
                new_path=plan.new_rel,
                size=facts.size,
                modified=facts.mtime,
            )
            if merged is None:
                report.unresolved_orphans.append(
                    OrphanInfo(
                        doc_id=plan.old_row["id"],
                        recorded_path=plan.old_rel,
                        hash=plan.old_row["hash"],
                        size=plan.old_row["size"],
                        reason="path_conflict_different_hash",
                    )
                )
                continue
            winner_id, loser_id = merged
            affected_hashes.add(plan.old_row["hash"])
            report.merges.append(
                MergeRecord(
                    old_path=plan.old_rel,
                    new_path=plan.new_rel,
                    winner_id=winner_id,
                    loser_id=loser_id,
                    hash=plan.old_row["hash"],
                )
            )
        for rel in sorted(unresolved):
            row = rows_by_path[rel]
            report.unresolved_orphans.append(
                OrphanInfo(doc_id=row["id"], recorded_path=rel, hash=row["hash"], size=row["size"])
            )
        for rel in sorted(both):
            row = rows_by_path[rel]
            facts = fs_facts[rel]
            if fresh_hashes.get(rel) == row["hash"] and not _row_facts_match(row, facts):
                conn.execute(
                    "UPDATE docs SET size = ?, modified = ? WHERE id = ?",
                    (facts.size, facts.mtime, row["id"]),
                )
        if affected_hashes:
            _recompute_dups_for_hashes(conn, affected_hashes)
        post_rows = _fetch_rows_by_path(conn)
    return report, post_rows


def _candidate_for(row: sqlite3.Row | None, facts: FileFacts, file_hash: str, reason: str, **details: Any) -> DigestCandidate:
    merged_details = dict(facts.details)
    merged_details.update(details)
    return DigestCandidate(
        rel=facts.rel,
        size=facts.size,
        mtime=facts.mtime,
        file_hash=file_hash,
        reason=reason,
        doc_id=row["id"] if row is not None else None,
        details=merged_details,
    )


def _build_worklist(
    *,
    fs_facts: dict[str, FileFacts],
    rows_by_path: dict[str, sqlite3.Row],
    fresh_hashes: dict[str, str],
    force: bool,
) -> WorklistForDigest:
    settings = get_settings()
    current_engine = _current_tesseract_version()
    current_langs = settings.tesseract.langs
    worklist = WorklistForDigest()
    for rel, facts in sorted(fs_facts.items()):
        row = rows_by_path.get(rel)
        if row is None:
            file_hash = fresh_hashes.get(rel)
            if file_hash is None:
                file_hash = hash_file(facts.path)
                fresh_hashes[rel] = file_hash
            worklist.new_files.append(_candidate_for(row, facts, file_hash, "no_record"))
            continue

        file_hash = fresh_hashes.get(rel) or row["hash"]
        if force:
            worklist.changed_files.append(_candidate_for(row, facts, file_hash, "force"))
            continue
        if fresh_hashes.get(rel) and fresh_hashes[rel] != row["hash"]:
            worklist.changed_files.append(_candidate_for(row, facts, file_hash, "content_changed"))
            continue

        partial = _is_partial(row)
        if partial:
            worklist.partial_metadata.append(
                _candidate_for(row, facts, file_hash, "partial_metadata", missing_fields=partial)
            )
        stale, delta_days = _is_stale(row, facts)
        if stale:
            worklist.stale_metadata.append(
                _candidate_for(
                    row,
                    facts,
                    file_hash,
                    "stale_metadata",
                    metadata_modified=row["modified"],
                    file_modified=facts.mtime,
                    delta_days=delta_days,
                )
            )
        if _ocr_drift(row, current_engine, current_langs):
            worklist.ocr_review.append(
                _candidate_for(
                    row,
                    facts,
                    file_hash,
                    "ocr_drift",
                    previous_engine=row["ocr_engine_ver"],
                    previous_languages=row["ocr_langs"],
                    current_engine=current_engine,
                    current_languages=current_langs,
                )
            )
    return worklist


def run_scan(
    docs: Path | None = None,
    subpath: Path | None = None,
    *,
    apply: bool = True,
    force: bool = False,
    rehash: bool = False,
) -> ScanReport:
    settings = get_settings()
    root = docs or settings.docs
    if not root.exists():
        root.mkdir(parents=True, exist_ok=True)
    walk_root = _resolve_walk_root(root, subpath)
    walk_prefix = _walk_prefix(root, walk_root, subpath)

    log.info("scan starting under %s", walk_root)
    observe_tree(walk_root, include_root=subpath is not None)
    rows_by_path = _index_existing_rows()
    scoped_rows = _rows_in_scope(rows_by_path, walk_prefix)
    fs_facts, files_seen, skipped, unprocessable = _inventory(root, walk_root, rows_by_path)

    fs_paths = set(fs_facts)
    db_paths = set(scoped_rows)
    fs_only = fs_paths - db_paths
    db_only = {
        rel
        for rel in db_paths - fs_paths
        if not (root / rel).exists()
    }
    both = fs_paths & db_paths
    fresh_hashes: dict[str, str] = {}

    known_plans, unresolved = _plan_known_hash_renames(
        fs_facts=fs_facts,
        rows_by_path=scoped_rows,
        db_only=db_only,
        fs_only=fs_only,
        both=both,
        fresh_hashes=fresh_hashes,
    )
    claimed_targets = {plan.new_rel for plan in known_plans}
    _selective_rehash(
        fs_facts=fs_facts,
        rows_by_path=scoped_rows,
        fs_only=fs_only,
        both=both,
        resolved_targets=claimed_targets,
        fresh_hashes=fresh_hashes,
        rehash=rehash,
    )
    fresh_plans, unresolved = _plan_fresh_hash_renames(
        rows_by_path=scoped_rows,
        fs_facts=fs_facts,
        unresolved=unresolved,
        fresh_hashes=fresh_hashes,
        claimed_targets=claimed_targets,
    )
    plans = known_plans + fresh_plans
    report, post_rows = _apply_reconciliation(
        plans=plans,
        unresolved=unresolved,
        fs_facts=fs_facts,
        fresh_hashes=fresh_hashes,
        both=both,
        rows_by_path=scoped_rows,
        apply=apply,
    )
    scoped_post_rows = _rows_in_scope(post_rows, walk_prefix)
    worklist = _build_worklist(
        fs_facts=fs_facts,
        rows_by_path=scoped_post_rows,
        fresh_hashes=fresh_hashes,
        force=force,
    )
    worklist.unprocessable.extend(unprocessable)
    scan_report = ScanReport(
        ran_at=utc_now_iso(),
        docs_root=str(root),
        files_seen=files_seen,
        skipped=skipped,
        worklist=worklist,
        reconciliation=report,
        tree_size=files_seen,
    )
    log.info(
        "scan complete under %s: seen=%d skipped=%d new=%d changed=%d partial=%d "
        "stale=%d ocr_review=%d missing=%d unprocessable=%d",
        walk_root, scan_report.files_seen, scan_report.skipped,
        len(worklist.new_files), len(worklist.changed_files), len(worklist.partial_metadata),
        len(worklist.stale_metadata), len(worklist.ocr_review),
        len(report.unresolved_orphans), len(worklist.unprocessable),
    )
    return scan_report
