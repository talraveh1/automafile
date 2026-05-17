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
    assert "Not found" in result.output


def test_rm_force_ignores_missing(docs_root):
    target = docs_root / "Inbox" / "ghost.txt"
    result = runner.invoke(app, ["rm", "-f", str(target)])
    assert result.exit_code == 0, result.output


def test_rm_default_uses_recycle_bin(docs_root, monkeypatch):
    """Without --purge, deletion should route through send2trash."""
    target = docs_root / "Inbox" / "recycle.txt"
    _seed(target, "via trash")

    called: list[str] = []

    def fake_send2trash(p: str) -> None:
        called.append(p)
        Path(p).unlink()  # simulate the file being whisked away to the bin

    monkeypatch.setattr("send2trash.send2trash", fake_send2trash)

    result = runner.invoke(app, ["rm", str(target)])
    assert result.exit_code == 0, result.output
    assert called == [str(target)]
    assert not target.exists()
    assert get_by_file(target) is None


def test_rm_purge_bypasses_recycle_bin(docs_root, monkeypatch):
    target = docs_root / "Inbox" / "permanent.txt"
    _seed(target, "purge me")

    def fail_if_called(_p: str) -> None:
        raise AssertionError("send2trash should not be called when --purge is set")

    monkeypatch.setattr("send2trash.send2trash", fail_if_called)

    result = runner.invoke(app, ["rm", "-P", str(target)])
    assert result.exit_code == 0, result.output
    assert not target.exists()
    assert get_by_file(target) is None


def test_rm_recycle_bin_failure_reports_error(docs_root, monkeypatch):
    """A send2trash failure surfaces a clear error and does not fall back."""
    target = docs_root / "Inbox" / "stuck.txt"
    _seed(target, "stuck")

    def boom(_p: str) -> None:
        raise OSError("no recycle bin on this volume")

    monkeypatch.setattr("send2trash.send2trash", boom)

    result = runner.invoke(app, ["rm", str(target)])
    assert result.exit_code != 0
    assert "recycle-bin move failed" in result.output
    assert target.exists()
    assert get_by_file(target) is not None
