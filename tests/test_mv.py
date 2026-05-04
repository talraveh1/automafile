"""Tests for the `dnd mv` CLI helper."""

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


def test_mv_moves_file_and_updates_row(docs_root):
    src = docs_root / "Inbox" / "note.txt"
    _seed(src, "hello")
    dst_dir = docs_root / "Personal"
    dst_dir.mkdir()
    dst = dst_dir / "renamed.txt"

    result = runner.invoke(app, ["mv", str(src), str(dst)])
    assert result.exit_code == 0, result.output
    assert dst.exists()
    assert not src.exists()
    doc = get_by_file(dst)
    assert doc is not None
    assert doc.path.endswith("Personal/renamed.txt")


def test_mv_into_directory_appends_basename(docs_root):
    src = docs_root / "Inbox" / "note.txt"
    _seed(src, "hello")
    dst_dir = docs_root / "Personal"
    dst_dir.mkdir()

    result = runner.invoke(app, ["mv", str(src), str(dst_dir)])
    assert result.exit_code == 0, result.output
    assert (dst_dir / "note.txt").exists()
    assert get_by_file(dst_dir / "note.txt") is not None


def test_mv_fails_when_target_exists(docs_root):
    src = docs_root / "Inbox" / "note.txt"
    _seed(src, "hello")
    dst = docs_root / "elsewhere.txt"
    dst.write_text("squatter", encoding="utf-8")

    result = runner.invoke(app, ["mv", str(src), str(dst)])
    assert result.exit_code != 0
    assert "target exists" in result.output
    assert src.exists()
    assert dst.read_text(encoding="utf-8") == "squatter"


def test_mv_force_overwrites_target(docs_root):
    src = docs_root / "Inbox" / "note.txt"
    _seed(src, "fresh")
    dst = docs_root / "elsewhere.txt"
    dst.write_text("stale", encoding="utf-8")

    result = runner.invoke(app, ["mv", "-f", str(src), str(dst)])
    assert result.exit_code == 0, result.output
    assert dst.read_text(encoding="utf-8") == "fresh"
    assert not src.exists()
    assert get_by_file(dst) is not None


def test_mv_without_row_still_moves_file(docs_root):
    src = docs_root / "Inbox" / "bare.txt"
    src.write_text("no row", encoding="utf-8")
    dst = docs_root / "moved.txt"

    result = runner.invoke(app, ["mv", str(src), str(dst)])
    assert result.exit_code == 0, result.output
    assert dst.exists()
    assert not src.exists()


def test_mv_missing_src_errors(docs_root):
    src = docs_root / "Inbox" / "ghost.txt"
    dst = docs_root / "anywhere.txt"
    result = runner.invoke(app, ["mv", str(src), str(dst)])
    assert result.exit_code != 0
    assert "src not found" in result.output
