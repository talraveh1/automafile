"""Duplicate-state tests."""

from __future__ import annotations

from dragndoc.meta_store import Doc, get_by_path, recompute_dups, set_dup, upsert
from dragndoc.triage import dequeue_by_path, list_queue


def _doc(path: str, hash_value: str, *, dup: str = "unique") -> Doc:
    return Doc(path=path, hash=hash_value, size=1, original=path.rsplit("/", 1)[-1], dup=dup)


def test_recompute_dups_promotes_unique_to_dup(docs_root):
    upsert(_doc("Inbox/a.txt", "sha256:same"))
    upsert(_doc("Inbox/b.txt", "sha256:same"))
    recompute_dups()
    assert get_by_path("Inbox/a.txt").dup == "dup"
    assert get_by_path("Inbox/b.txt").dup == "dup"


def test_recompute_dups_demotes_solo_to_unique(docs_root):
    upsert(_doc("Inbox/a.txt", "sha256:same", dup="keep"))
    recompute_dups()
    assert get_by_path("Inbox/a.txt").dup == "unique"


def test_recompute_dups_preserves_keep_group(docs_root):
    upsert(_doc("Inbox/a.txt", "sha256:same", dup="keep"))
    upsert(_doc("Inbox/b.txt", "sha256:same"))
    recompute_dups()
    assert get_by_path("Inbox/a.txt").dup == "keep"
    assert get_by_path("Inbox/b.txt").dup == "dup"


def test_set_dup_propagates_outside_inbox_and_defers_inbox(docs_root):
    upsert(_doc("Filed/a.txt", "sha256:same", dup="dup"))
    upsert(_doc("Filed/b.txt", "sha256:same", dup="dup"))
    upsert(_doc("Inbox/c.txt", "sha256:same", dup="dup"))
    result = set_dup(docs_root / "Filed" / "a.txt", "keep")
    assert result.siblings_changed == ["Filed/b.txt"]
    assert result.inbox_deferred == ["Inbox/c.txt"]
    assert get_by_path("Filed/a.txt").dup == "keep"
    assert get_by_path("Filed/b.txt").dup == "keep"
    assert get_by_path("Inbox/c.txt").dup == "dup"


def test_triage_lists_synthetic_dup_entries(docs_root):
    upsert(_doc("Inbox/a.txt", "sha256:same"))
    upsert(_doc("Inbox/b.txt", "sha256:same"))
    recompute_dups()
    entries = list_queue(inbox_only=True)
    assert sorted((entry.doc.path, entry.reason) for entry in entries) == [
        ("Inbox/a.txt", "duplicate"),
        ("Inbox/b.txt", "duplicate"),
    ]


def test_triage_done_does_not_clear_synthetic_dup(docs_root):
    upsert(_doc("Inbox/a.txt", "sha256:same"))
    upsert(_doc("Inbox/b.txt", "sha256:same"))
    recompute_dups()
    assert dequeue_by_path("Inbox/a.txt") is False
    assert any(entry.doc.path == "Inbox/a.txt" for entry in list_queue(inbox_only=True))
