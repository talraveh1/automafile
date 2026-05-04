"""Tests for the `dnd rm` CLI helper."""

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


def test_rm_removes_file_and_row(docs_root):
    target = docs_root / "Inbox" / "note.txt"
    _seed(target, "hello")
    assert get_by_file(target) is not None

    result = runner.invoke(app, ["rm", str(target)])
    assert result.exit_code == 0, result.output
    assert not target.exists()
    assert get_by_file(target) is None


def test_rm_without_row_still_removes_file(docs_root):
    target = docs_root / "Inbox" / "bare.txt"
    target.write_text("no row", encoding="utf-8")

    result = runner.invoke(app, ["rm", str(target)])
    assert result.exit_code == 0, result.output
    assert not target.exists()


def test_rm_missing_errors(docs_root):
    target = docs_root / "Inbox" / "ghost.txt"
    result = runner.invoke(app, ["rm", str(target)])
    assert result.exit_code != 0
    assert "not found" in result.output


def test_rm_force_ignores_missing(docs_root):
    target = docs_root / "Inbox" / "ghost.txt"
    result = runner.invoke(app, ["rm", "-f", str(target)])
    assert result.exit_code == 0, result.output
