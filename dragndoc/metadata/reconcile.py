"""Find orphaned ``docs`` rows whose file no longer exists; suggest hash-matched relinks.

An "orphan" in the DB-backed world is a row whose ``path`` doesn't resolve
to a file on disk (typical cause: the file was moved or renamed outside
``dnd mv``). For each orphan we check whether the same content lives at
some other path — first by stat-size pre-filter, then by hash — and offer
to update the row.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from dragndoc.config import get_settings
from dragndoc.db import connect, transaction
from dragndoc.log import get_logger
from dragndoc.metadata.hashing import hash_file
from dragndoc.treewalk import iter_unblocked_files


log = get_logger(__name__)


@dataclass
class OrphanReport:
    doc_id: int
    recorded_path: str
    expected_hash: str
    expected_size: int
    matches_in_tree: list[Path] = field(default_factory=list)


def find_orphans(root: Path | None = None) -> list[OrphanReport]:
    """Return rows whose ``path`` doesn't resolve to a file on disk.

    For each, walk ``root`` (defaulting to ``documents_root``) and report
    any files whose size matches the recorded ``size`` AND hash matches
    the recorded ``hash``. The size check first lets us skip almost every
    file in a typical tree; only size-matching candidates get hashed.
    """
    settings = get_settings()
    walk_root = root or settings.documents_root

    with connect(readonly=True) as conn:
        rows = conn.execute("SELECT id, path, hash, size FROM docs").fetchall()

    missing: list[OrphanReport] = []
    sizes_to_check: dict[int, list[OrphanReport]] = {}
    for r in rows:
        full = settings.documents_root / r["path"]
        if full.exists():
            continue
        report = OrphanReport(
            doc_id=int(r["id"]),
            recorded_path=str(r["path"]),
            expected_hash=str(r["hash"]),
            expected_size=int(r["size"]),
        )
        missing.append(report)
        sizes_to_check.setdefault(report.expected_size, []).append(report)

    if not missing:
        log.debug("find_orphans: no orphans under %s", walk_root)
        return missing

    # Walk once; only hash files whose size matches at least one orphan.
    for path in iter_unblocked_files(walk_root):
        try:
            size = path.stat().st_size
        except OSError:
            continue
        candidates = sizes_to_check.get(size)
        if not candidates:
            continue
        try:
            h = hash_file(path)
        except OSError:
            continue
        for report in candidates:
            if h == report.expected_hash:
                report.matches_in_tree.append(path)

    log.info(
        "find_orphans: %d orphan row(s); %d had hash matches",
        len(missing), sum(1 for r in missing if r.matches_in_tree),
    )
    return missing


def relink(doc_id: int, new_path: Path) -> None:
    """Update an orphan row to point at ``new_path`` (relative to ``documents_root``)."""
    from dragndoc.meta_store import relative_to_root

    rel = relative_to_root(new_path)
    with transaction() as conn:
        conn.execute("UPDATE docs SET path = ? WHERE id = ?", (rel, doc_id))
    log.info("relinked doc id=%d -> %s", doc_id, rel)
