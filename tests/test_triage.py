"""Triage queue tests."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from dragndoc.cli import app
from dragndoc.dirs import ensure_tracked
from dragndoc.meta_store import Doc, OcrInfo, relative_to_root, upsert
from dragndoc.metadata.hashing import hash_file
from dragndoc.triage import count, enqueue, list_queue


runner = CliRunner()


def _seed(path: Path, body: str) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return upsert(
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


def test_list_queue_collapses_inbox_collection_to_single_entry(docs_root):
    collection = docs_root / "Inbox" / "Case"
    first_id = _seed(collection / "a.txt", "alpha")
    second_id = _seed(collection / "nested" / "b.txt", "beta")
    ensure_tracked(collection)

    enqueue(first_id)
    enqueue(second_id)

    entries = list_queue(inbox_only=True)

    assert len(entries) == 1
    assert count(inbox_only=True) == 1
    assert entries[0].scope_kind == "collection"
    assert entries[0].scope_path == "Inbox/Case"
    assert entries[0].member_count == 2
    assert entries[0].doc.path == "Inbox/Case/a.txt"


def test_triage_done_clears_collection_after_directory_move(docs_root):
    collection = docs_root / "Inbox" / "Case"
    first_id = _seed(collection / "a.txt", "alpha")
    second_id = _seed(collection / "nested" / "b.txt", "beta")
    ensure_tracked(collection)

    enqueue(first_id)
    enqueue(second_id)

    target_parent = docs_root / "Legal"
    target_parent.mkdir()

    move_result = runner.invoke(app, ["mv", "-y", str(collection), str(target_parent)])
    assert move_result.exit_code == 0, move_result.output

    moved_root = target_parent / "Case"
    done_result = runner.invoke(app, ["triage", "done", str(moved_root)])
    assert done_result.exit_code == 0, done_result.output
    assert count(inbox_only=False) == 0