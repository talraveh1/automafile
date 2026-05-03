"""Tests for the `dnd cp` CLI helper."""

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


def test_cp_copies_file_and_sidecar(docs_root):
    src = docs_root / "Inbox" / "note.txt"
    _seed(src, "hello")
    dst_dir = docs_root / "Personal"
    dst_dir.mkdir()
    dst = dst_dir / "renamed.txt"

    result = runner.invoke(app, ["cp", str(src), str(dst)])
    assert result.exit_code == 0, result.output
    assert dst.exists()
    assert src.exists()
    new_sidecar = sidecar.sidecar_path_for(dst)
    old_sidecar = sidecar.sidecar_path_for(src)
    assert new_sidecar.exists()
    assert old_sidecar.exists()
    doc, _, _ = sidecar.read(dst)
    assert doc is not None
    assert doc.relative_path.endswith("Personal/renamed.txt")
    src_doc, _, _ = sidecar.read(src)
    assert src_doc is not None
    assert src_doc.relative_path == "note.txt"


def test_cp_into_directory_appends_basename(docs_root):
    src = docs_root / "Inbox" / "note.txt"
    _seed(src, "hello")
    dst_dir = docs_root / "Personal"
    dst_dir.mkdir()

    result = runner.invoke(app, ["cp", str(src), str(dst_dir)])
    assert result.exit_code == 0, result.output
    assert (dst_dir / "note.txt").exists()
    assert src.exists()
    assert sidecar.sidecar_path_for(dst_dir / "note.txt").exists()


def test_cp_fails_when_target_exists(docs_root):
    src = docs_root / "Inbox" / "note.txt"
    _seed(src, "hello")
    dst = docs_root / "elsewhere.txt"
    dst.write_text("squatter", encoding="utf-8")

    result = runner.invoke(app, ["cp", str(src), str(dst)])
    assert result.exit_code != 0
    assert "target exists" in result.output
    assert dst.read_text(encoding="utf-8") == "squatter"


def test_cp_fails_when_target_sidecar_exists(docs_root):
    src = docs_root / "Inbox" / "note.txt"
    _seed(src, "hello")
    dst = docs_root / "elsewhere.txt"
    sidecar_dir = dst.parent / ".meta"
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    (sidecar_dir / f"{dst.name}.md").write_text("---\nbogus: 1\n---\n", encoding="utf-8")

    result = runner.invoke(app, ["cp", str(src), str(dst)])
    assert result.exit_code != 0
    assert "target sidecar exists" in result.output
    assert not dst.exists()


def test_cp_force_overwrites_target_and_sidecar(docs_root):
    src = docs_root / "Inbox" / "note.txt"
    _seed(src, "fresh")
    dst = docs_root / "elsewhere.txt"
    dst.write_text("stale", encoding="utf-8")
    sidecar_dir = dst.parent / ".meta"
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    (sidecar_dir / f"{dst.name}.md").write_text("stale meta", encoding="utf-8")

    result = runner.invoke(app, ["cp", "-f", str(src), str(dst)])
    assert result.exit_code == 0, result.output
    assert dst.read_text(encoding="utf-8") == "fresh"
    assert src.exists()
    doc, _, _ = sidecar.read(dst)
    assert doc is not None


def test_cp_without_sidecar_still_copies_file(docs_root):
    src = docs_root / "Inbox" / "bare.txt"
    src.write_text("no sidecar", encoding="utf-8")
    dst = docs_root / "copy.txt"

    result = runner.invoke(app, ["cp", str(src), str(dst)])
    assert result.exit_code == 0, result.output
    assert dst.exists()
    assert src.exists()
    assert not sidecar.sidecar_path_for(dst).exists()


def test_cp_missing_src_errors(docs_root):
    src = docs_root / "Inbox" / "ghost.txt"
    dst = docs_root / "anywhere.txt"
    result = runner.invoke(app, ["cp", str(src), str(dst)])
    assert result.exit_code != 0
    assert "src not found" in result.output


def test_cp_same_src_and_dst_errors(docs_root):
    src = docs_root / "Inbox" / "note.txt"
    _seed(src, "hello")
    result = runner.invoke(app, ["cp", str(src), str(src)])
    assert result.exit_code != 0
    assert "same" in result.output
