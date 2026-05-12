"""Tests for the `dnd ls` and `dnd meta cat` CLI helpers."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from dragndoc.cli import app
from dragndoc.meta_store import Doc, OcrInfo, relative_to_root, upsert
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


def test_ls_marks_files_with_rows(docs_root):
    inbox = docs_root / "Inbox"
    with_meta = inbox / "with_meta.txt"
    bare = inbox / "bare.txt"
    _seed(with_meta, "has row")
    bare.write_text("no row", encoding="utf-8")
    (inbox / "Subfolder").mkdir()

    result = runner.invoke(app, ["ls", str(inbox)])
    assert result.exit_code == 0, result.output
    lines = [line for line in result.output.splitlines() if line]
    assert any(line.endswith("Subfolder/") for line in lines)
    assert any(line.startswith("*") and "with_meta.txt" in line for line in lines)
    assert any(line.startswith(" ") and "bare.txt" in line for line in lines)


def test_ls_default_to_cwd(docs_root, monkeypatch):
    inbox = docs_root / "Inbox"
    _seed(inbox / "alpha.txt", "hi")
    monkeypatch.chdir(inbox)
    result = runner.invoke(app, ["ls"])
    assert result.exit_code == 0, result.output
    assert "alpha.txt" in result.output


def test_ls_missing_path_errors(docs_root):
    target = docs_root / "ghost"
    result = runner.invoke(app, ["ls", str(target)])
    assert result.exit_code != 0
    assert "Not found" in result.output


def test_ls_on_file_errors(docs_root):
    target = docs_root / "Inbox" / "note.txt"
    target.write_text("hi", encoding="utf-8")
    result = runner.invoke(app, ["ls", str(target)])
    assert result.exit_code != 0
    assert "Not a directory" in result.output


def test_meta_cat_renders_markdown(docs_root):
    target = docs_root / "Inbox" / "note.txt"
    _seed(target, "hello")

    result = runner.invoke(app, ["meta", "cat", str(target)])
    assert result.exit_code == 0, result.output
    assert "---" in result.output
    assert "hash:" in result.output
    assert "seeded" in result.output


def test_meta_cat_no_row_errors(docs_root):
    target = docs_root / "Inbox" / "bare.txt"
    target.write_text("no row", encoding="utf-8")
    result = runner.invoke(app, ["meta", "cat", str(target)])
    assert result.exit_code != 0
    assert "No row" in result.output


def test_meta_get_returns_json(docs_root):
    target = docs_root / "Inbox" / "note.txt"
    _seed(target, "hello")
    result = runner.invoke(app, ["meta", "get", str(target)])
    assert result.exit_code == 0, result.output
    import json
    payload = json.loads(result.output)
    assert payload["path"].endswith("Inbox/note.txt")
    assert payload["category"] == "Personal"
