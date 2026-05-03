"""Tests for the `dnd ls` and `dnd cat` CLI helpers."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from dragndoc.cli import app
from dragndoc.metadata import sidecar
from dragndoc.metadata.hashing import hash_file
from dragndoc.metadata.schema import MetadataDoc, OcrBlock


runner = CliRunner()


def _seed(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    meta = MetadataDoc(
        content_hash=hash_file(path),
        file_size=path.stat().st_size,
        filename_at_creation=path.name,
        relative_path=str(path.name),
        category="Personal",
        ocr=OcrBlock(decision="never"),
    )
    sidecar.write(path, meta, summary_body="seeded")


def test_ls_marks_sidecared_files(docs_root):
    inbox = docs_root / "Inbox"
    sidecared = inbox / "with_meta.txt"
    bare = inbox / "bare.txt"
    _seed(sidecared, "has sidecar")
    bare.write_text("no sidecar", encoding="utf-8")
    (inbox / "Subfolder").mkdir()

    result = runner.invoke(app, ["ls", str(inbox)])
    assert result.exit_code == 0, result.output
    lines = [line for line in result.output.splitlines() if line]
    # Subfolder appears as a directory entry, sidecar storage is hidden
    assert any(line.endswith("Subfolder/") for line in lines)
    assert any(line.startswith("*") and "with_meta.txt" in line for line in lines)
    assert any(line.startswith(" ") and "bare.txt" in line for line in lines)
    # the .meta directory should not show up
    assert not any(".meta" in line for line in lines)


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
    assert "not found" in result.output


def test_ls_on_file_errors(docs_root):
    target = docs_root / "Inbox" / "note.txt"
    target.write_text("hi", encoding="utf-8")
    result = runner.invoke(app, ["ls", str(target)])
    assert result.exit_code != 0
    assert "not a directory" in result.output


def test_cat_prints_sidecar_for_file(docs_root):
    target = docs_root / "Inbox" / "note.txt"
    _seed(target, "hello")

    result = runner.invoke(app, ["cat", str(target)])
    assert result.exit_code == 0, result.output
    # frontmatter + summary body should both appear
    assert "---" in result.output
    assert "content_hash:" in result.output
    assert "seeded" in result.output


def test_cat_no_sidecar_errors(docs_root):
    target = docs_root / "Inbox" / "bare.txt"
    target.write_text("no sidecar", encoding="utf-8")
    result = runner.invoke(app, ["cat", str(target)])
    assert result.exit_code != 0
    assert "no sidecar" in result.output
