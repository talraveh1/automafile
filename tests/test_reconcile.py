"""Hash-based reconciliation tests."""

from __future__ import annotations

import shutil
from pathlib import Path

from automafile.metadata import sidecar
from automafile.metadata.hashing import hash_file
from automafile.metadata.reconcile import find_orphans
from automafile.metadata.schema import MetadataDoc, OcrBlock


def _seed_with_sidecar(file_path: Path, payload: bytes) -> None:
    file_path.write_bytes(payload)
    meta = MetadataDoc(
        content_hash=hash_file(file_path),
        file_size=file_path.stat().st_size,
        filename_at_creation=file_path.name,
        relative_path=str(file_path.name),
        category="Personal",
        ocr=OcrBlock(decision="never"),
    )
    sidecar.write(file_path, meta, summary_body="hello")


def test_orphan_detected_when_target_missing(docs_root):
    path = docs_root / "Inbox" / "doc.txt"
    _seed_with_sidecar(path, b"hello world")
    sidecar_file = sidecar.sidecar_path_for(path)
    assert sidecar_file.exists()
    path.unlink()
    orphans = find_orphans(docs_root)
    assert len(orphans) == 1
    assert orphans[0].sidecar_path == sidecar_file


def test_orphan_with_hash_match_in_tree(docs_root):
    src = docs_root / "Inbox" / "doc.txt"
    payload = b"the quick brown fox jumps over the lazy dog"
    _seed_with_sidecar(src, payload)
    moved = docs_root / "Personal" / "doc.txt"
    moved.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(moved))
    orphans = find_orphans(docs_root)
    assert orphans
    matches = orphans[0].matches_in_tree
    assert any(p.resolve() == moved.resolve() for p in matches)
