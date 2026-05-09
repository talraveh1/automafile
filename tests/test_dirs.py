"""Tests for directory-mode metadata and directory-aware CLI operations."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from dragndoc.cli import app
from dragndoc.dirs import get_dir
from dragndoc.meta_store import Doc, OcrInfo, get_by_file, relative_to_root, upsert
from dragndoc.metadata.hashing import hash_file
from dragndoc.scanner import run_scan


runner = CliRunner()


def _seed(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    upsert(
        Doc(
            path=relative_to_root(path),
            hash=hash_file(path),
            size=path.stat().st_size,
            original=path.name,
            category="Personal",
            summary="seeded",
            ocr=OcrInfo(decision="never"),
        )
    )


def test_dir_get_auto_tracks_collection_and_opaque(docs_root):
    project = docs_root / "Inbox" / "Project"
    project.mkdir()
    node_modules = project / "node_modules"
    node_modules.mkdir()

    result = runner.invoke(app, ["dir", "get", str(project)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["path"] == "Inbox/Project"
    assert payload["mode"] == "collection"

    result = runner.invoke(app, ["dir", "get", str(node_modules)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["mode"] == "opaque"
    assert payload["source"] == "hardcoded"


def test_dir_set_overrides_hardcoded_mode(docs_root):
    target = docs_root / "Inbox" / "node_modules"
    target.mkdir()

    result = runner.invoke(app, ["dir", "set", str(target), "--mode", "collection"])
    assert result.exit_code == 0, result.output
    row = get_dir(target)
    assert row is not None
    assert row.mode == "collection"
    assert row.source == "user"


def test_scan_records_directories_and_skips_opaque_subtrees(docs_root):
    keep = docs_root / "Inbox" / "Project" / "keep.txt"
    keep.parent.mkdir(parents=True)
    keep.write_text("keep", encoding="utf-8")
    skipped = docs_root / "Inbox" / "Project" / "node_modules" / "pkg" / "index.txt"
    skipped.parent.mkdir(parents=True)
    skipped.write_text("skip", encoding="utf-8")

    report = run_scan()
    rels = [entry["relative_path"] for entry in report.files_needing_metadata]

    project = get_dir(docs_root / "Inbox" / "Project")
    node_modules = get_dir(docs_root / "Inbox" / "Project" / "node_modules")
    assert project is not None
    assert project.mode == "collection"
    assert node_modules is not None
    assert node_modules.mode == "opaque"
    assert "Inbox/Project/keep.txt" in rels
    assert not any("node_modules" in rel for rel in rels)


def test_ls_prints_directory_mode_tags(docs_root):
    inbox = docs_root / "Inbox"
    (inbox / "Case").mkdir()
    (inbox / "node_modules").mkdir()

    result = runner.invoke(app, ["ls", "-a", str(inbox)])
    assert result.exit_code == 0, result.output
    assert "[col] Case/" in result.output
    assert "[opq] node_modules/" in result.output


def test_mv_directory_rewrites_doc_and_dir_paths(docs_root):
    src_dir = docs_root / "Inbox" / "Case"
    doc_path = src_dir / "note.txt"
    _seed(doc_path, "hello")
    runner.invoke(app, ["dir", "get", str(src_dir)])
    dst_dir = docs_root / "Legal"
    dst_dir.mkdir()

    result = runner.invoke(app, ["mv", "-y", str(src_dir), str(dst_dir)])
    assert result.exit_code == 0, result.output

    moved = dst_dir / "Case" / "note.txt"
    assert moved.exists()
    assert get_by_file(moved) is not None
    assert get_by_file(moved).path == "Legal/Case/note.txt"
    assert get_dir(docs_root / "Legal" / "Case") is not None
    assert get_dir(src_dir) is None


def test_rm_directory_metadata_only_removes_cascade_rows(docs_root):
    src_dir = docs_root / "Inbox" / "Case"
    doc_path = src_dir / "note.txt"
    _seed(doc_path, "hello")
    runner.invoke(app, ["dir", "get", str(src_dir)])

    result = runner.invoke(app, ["rm", "-y", "--metadata-only", str(src_dir)])
    assert result.exit_code == 0, result.output
    assert doc_path.exists()
    assert get_by_file(doc_path) is None
    assert get_dir(src_dir) is None
