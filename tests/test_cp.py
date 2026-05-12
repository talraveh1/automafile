"""Tests for the `dnd cp` CLI helper."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from dragndoc.cli import app
from dragndoc.meta_store import Doc, OcrInfo, get_by_file, relative_to_root, upsert
from dragndoc.metadata.hashing import hash_file


runner = CliRunner()


def _seed(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    upsert(Doc(
        path=relative_to_root(path),
        hash=hash_file(path),
        size=path.stat().st_size,
        original=path.name,
        category="Personal",
        summary="seeded",
        ocr=OcrInfo(decision="never"),
    ))


def test_cp_copies_file_and_duplicates_row(docs_root):
    src = docs_root / "Inbox" / "note.txt"
    _seed(src, "hello")
    dst_dir = docs_root / "Personal"
    dst_dir.mkdir()
    dst = dst_dir / "renamed.txt"

    result = runner.invoke(app, ["cp", str(src), str(dst)])
    assert result.exit_code == 0, result.output
    assert dst.exists()
    assert src.exists()
    src_doc = get_by_file(src)
    dst_doc = get_by_file(dst)
    assert src_doc is not None
    assert dst_doc is not None
    assert dst_doc.path.endswith("Personal/renamed.txt")
    assert src_doc.path == "Inbox/note.txt"


def test_cp_into_directory_appends_basename(docs_root):
    src = docs_root / "Inbox" / "note.txt"
    _seed(src, "hello")
    dst_dir = docs_root / "Personal"
    dst_dir.mkdir()

    result = runner.invoke(app, ["cp", str(src), str(dst_dir)])
    assert result.exit_code == 0, result.output
    assert (dst_dir / "note.txt").exists()
    assert src.exists()
    assert get_by_file(dst_dir / "note.txt") is not None


def test_cp_fails_when_target_exists(docs_root):
    src = docs_root / "Inbox" / "note.txt"
    _seed(src, "hello")
    dst = docs_root / "elsewhere.txt"
    dst.write_text("squatter", encoding="utf-8")

    result = runner.invoke(app, ["cp", str(src), str(dst)])
    assert result.exit_code != 0
    assert "Target exists" in result.output
    assert dst.read_text(encoding="utf-8") == "squatter"


def test_cp_force_overwrites_target(docs_root):
    src = docs_root / "Inbox" / "note.txt"
    _seed(src, "fresh")
    dst = docs_root / "elsewhere.txt"
    dst.write_text("stale", encoding="utf-8")

    result = runner.invoke(app, ["cp", "-f", str(src), str(dst)])
    assert result.exit_code == 0, result.output
    assert dst.read_text(encoding="utf-8") == "fresh"
    assert src.exists()
    assert get_by_file(dst) is not None


def test_cp_without_row_still_copies_file(docs_root):
    src = docs_root / "Inbox" / "bare.txt"
    src.write_text("no row", encoding="utf-8")
    dst = docs_root / "copy.txt"

    result = runner.invoke(app, ["cp", str(src), str(dst)])
    assert result.exit_code == 0, result.output
    assert dst.exists()
    assert src.exists()
    assert get_by_file(dst) is None


def test_cp_missing_src_errors(docs_root):
    src = docs_root / "Inbox" / "ghost.txt"
    dst = docs_root / "anywhere.txt"
    result = runner.invoke(app, ["cp", str(src), str(dst)])
    assert result.exit_code != 0
    assert "Source not found" in result.output


def test_cp_same_src_and_dst_errors(docs_root):
    src = docs_root / "Inbox" / "note.txt"
    _seed(src, "hello")
    result = runner.invoke(app, ["cp", str(src), str(src)])
    assert result.exit_code != 0
    assert "same" in result.output
