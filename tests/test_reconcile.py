"""Hash-based reconciliation tests against the DB-backed reconcile module."""

from __future__ import annotations

import shutil
from pathlib import Path

from dragndoc.meta_store import Doc, OcrInfo, get_by_path, relative_to_root, upsert
from dragndoc.metadata.hashing import hash_file
from dragndoc.metadata.reconcile import find_orphans, relink


def _seed_with_row(file_path: Path, payload: bytes) -> None:
    file_path.write_bytes(payload)
    upsert(Doc(
        path=relative_to_root(file_path),
        hash=hash_file(file_path),
        size=file_path.stat().st_size,
        original=file_path.name,
        category="Personal",
        summary="hello",
        ocr=OcrInfo(decision="never"),
    ))


def test_orphan_detected_when_target_missing(docs_root):
    path = docs_root / "Inbox" / "doc.txt"
    _seed_with_row(path, b"hello world")
    path.unlink()
    orphans = find_orphans(docs_root)
    assert len(orphans) == 1
    assert orphans[0].recorded_path.endswith("doc.txt")


def test_orphan_with_hash_match_in_tree(docs_root):
    src = docs_root / "Inbox" / "doc.txt"
    payload = b"the quick brown fox jumps over the lazy dog"
    _seed_with_row(src, payload)
    moved = docs_root / "Personal" / "doc.txt"
    moved.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(moved))
    orphans = find_orphans(docs_root)
    assert orphans
    matches = orphans[0].matches_in_tree
    assert any(p.resolve() == moved.resolve() for p in matches)


def test_relink_updates_row_path(docs_root):
    src = docs_root / "Inbox" / "doc.txt"
    _seed_with_row(src, b"hello")
    src_rel = relative_to_root(src)
    moved = docs_root / "Personal" / "doc.txt"
    moved.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(moved))
    orphans = find_orphans(docs_root)
    assert orphans
    relink(orphans[0].doc_id, moved)
    assert get_by_path(src_rel) is None
    assert get_by_path(relative_to_root(moved)) is not None


def test_no_orphans_when_everything_present(docs_root):
    path = docs_root / "Inbox" / "doc.txt"
    _seed_with_row(path, b"data")
    assert find_orphans(docs_root) == []
