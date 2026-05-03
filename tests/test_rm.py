"""Tests for the `dnd rm` CLI helper."""

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


def test_rm_removes_file_and_sidecar(docs_root):
    target = docs_root / "Inbox" / "note.txt"
    _seed(target, "hello")
    sc = sidecar.sidecar_path_for(target)
    assert sc.exists()

    result = runner.invoke(app, ["rm", str(target)])
    assert result.exit_code == 0, result.output
    assert not target.exists()
    assert not sc.exists()


def test_rm_without_sidecar_still_removes_file(docs_root):
    target = docs_root / "Inbox" / "bare.txt"
    target.write_text("no sidecar", encoding="utf-8")

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
