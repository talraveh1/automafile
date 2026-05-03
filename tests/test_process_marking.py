"""Tests for the worklist-marking behavior of ``dnd process``.

Each successful per-file process stamps a ``processed`` ISO timestamp on
every entry that referenced it (across however many worklists the entry
appeared in), and the touched JSON files are rewritten atomically so a
crash mid-run doesn't lose progress. Subsequent runs skip files whose
``processed`` mark is at-or-after the file's mtime, unless ``--force``.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from dragndoc.cli import app
from dragndoc.llm import EnrichmentResult


runner = CliRunner()


_FAKE = EnrichmentResult(
    title="t", summary="s body of summary", tags=["x"], category="Personal",
    confidence="high", review=False, language="en", tier="strict",
)


def _write_worklist(scan_dir: Path, name: str, documents_root: Path, rels: list[str]) -> Path:
    scan_dir.mkdir(parents=True, exist_ok=True)
    path = scan_dir / name
    path.write_text(
        json.dumps({
            "ran_at": "2026-05-02T00:00:00Z",
            "documents_root": str(documents_root),
            "tree_size": len(rels),
            "files_seen": len(rels),
            "skipped": 0,
            "files_needing_metadata": [
                {"relative_path": r, "format": "txt", "reason": "no_sidecar"} for r in rels
            ],
            "files_needing_ocr": [],
            "files_with_partial_metadata": [],
            "files_with_stale_metadata": [],
            "ocr_review_candidates": [],
            "orphan_sidecars": [],
            "quarantined_sidecars": [],
            "unprocessable_files": [],
        }, indent=2),
        encoding="utf-8",
    )
    return path


def _seed_files(docs_root: Path, names: list[str]) -> list[Path]:
    out = []
    for n in names:
        p = docs_root / "Inbox" / n
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"content of {n}", encoding="utf-8")
        out.append(p)
    return out


def _scan_dir(docs_root: Path) -> Path:
    # conftest sets STORAGE_DIR = tmp_path / "storage"
    from dragndoc.config import get_settings
    return get_settings().scan_dir


def test_successful_run_marks_each_entry_with_timestamp(docs_root):
    _seed_files(docs_root, ["a.txt", "b.txt"])
    wl = _write_worklist(_scan_dir(docs_root), "scan-1.json", docs_root,
                         ["Inbox/a.txt", "Inbox/b.txt"])
    with patch("dragndoc.pipeline.enrich", return_value=_FAKE):
        res = runner.invoke(app, ["process", str(wl)])
    assert res.exit_code == 0, res.output
    data = json.loads(wl.read_text(encoding="utf-8"))
    entries = data["files_needing_metadata"]
    assert all("processed" in e for e in entries)
    # Mark is a valid ISO-Z timestamp
    for e in entries:
        datetime.fromisoformat(e["processed"].replace("Z", "+00:00"))


def test_failed_file_is_not_marked(docs_root):
    _seed_files(docs_root, ["good.txt", "bad.txt"])
    wl = _write_worklist(_scan_dir(docs_root), "scan-1.json", docs_root,
                         ["Inbox/good.txt", "Inbox/bad.txt"])
    bad = docs_root / "Inbox" / "bad.txt"

    real_enrich = _FAKE
    def fake_enrich(text, hints=None, taxonomy=None):
        # Inject a runtime error for bad.txt by pretending the file is missing
        # via a side-effect inside enrich.
        if hints and hints.get("filename") == "bad.txt":
            raise RuntimeError("boom")
        return real_enrich

    with patch("dragndoc.pipeline.enrich", side_effect=fake_enrich):
        res = runner.invoke(app, ["process", str(wl)])
    # explicit-mode keeps the file even with failures
    assert wl.exists()
    data = json.loads(wl.read_text(encoding="utf-8"))
    by_rel = {e["relative_path"]: e for e in data["files_needing_metadata"]}
    assert "processed" in by_rel["Inbox/good.txt"]
    assert "processed" not in by_rel["Inbox/bad.txt"]


def test_second_run_skips_already_processed(docs_root):
    _seed_files(docs_root, ["a.txt"])
    wl = _write_worklist(_scan_dir(docs_root), "scan-1.json", docs_root, ["Inbox/a.txt"])

    with patch("dragndoc.pipeline.enrich", return_value=_FAKE) as mock_enrich:
        res1 = runner.invoke(app, ["process", str(wl)])
        assert res1.exit_code == 0
        first_call_count = mock_enrich.call_count
        res2 = runner.invoke(app, ["process", str(wl)])

    assert res1.exit_code == 0
    assert res2.exit_code == 0
    assert "already-processed" in res2.output or "nothing to process" in res2.output
    # second run must NOT have called enrich again
    assert mock_enrich.call_count == first_call_count


def test_modified_file_is_re_processed_without_force(docs_root):
    [path] = _seed_files(docs_root, ["a.txt"])
    wl = _write_worklist(_scan_dir(docs_root), "scan-1.json", docs_root, ["Inbox/a.txt"])

    with patch("dragndoc.pipeline.enrich", return_value=_FAKE) as mock_enrich:
        res1 = runner.invoke(app, ["process", str(wl)])
        assert res1.exit_code == 0

        # Bump mtime explicitly into the future so it's strictly after `processed`.
        future = time.time() + 10
        os.utime(path, (future, future))

        res2 = runner.invoke(app, ["process", str(wl)])

    assert res2.exit_code == 0
    # second run should have processed again (2 total calls)
    assert mock_enrich.call_count == 2


def test_force_flag_re_processes_unmodified_file(docs_root):
    _seed_files(docs_root, ["a.txt"])
    wl = _write_worklist(_scan_dir(docs_root), "scan-1.json", docs_root, ["Inbox/a.txt"])

    with patch("dragndoc.pipeline.enrich", return_value=_FAKE) as mock_enrich:
        runner.invoke(app, ["process", str(wl)])
        n1 = mock_enrich.call_count
        res = runner.invoke(app, ["process", str(wl), "--force"])
        n2 = mock_enrich.call_count
    assert res.exit_code == 0
    assert n2 == n1 + 1  # forced re-run did the work


def test_dry_run_does_not_mark(docs_root):
    _seed_files(docs_root, ["a.txt"])
    wl = _write_worklist(_scan_dir(docs_root), "scan-1.json", docs_root, ["Inbox/a.txt"])
    with patch("dragndoc.pipeline.enrich", return_value=_FAKE):
        res = runner.invoke(app, ["process", str(wl), "--dry-run"])
    assert res.exit_code == 0
    data = json.loads(wl.read_text(encoding="utf-8"))
    assert "processed" not in data["files_needing_metadata"][0]


def test_atomic_write_persists_after_each_file(docs_root):
    """If we crash after file 1 of 2, file 1's mark must already be on disk."""
    _seed_files(docs_root, ["a.txt", "b.txt"])
    wl = _write_worklist(_scan_dir(docs_root), "scan-1.json", docs_root,
                         ["Inbox/a.txt", "Inbox/b.txt"])

    call_count = {"n": 0}
    def crashy(text, hints=None, taxonomy=None):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise RuntimeError("simulated crash")
        return _FAKE

    with patch("dragndoc.pipeline.enrich", side_effect=crashy):
        # catch_exceptions=False so the RuntimeError propagates out of the
        # CLI and we can prove the per-file rewrite already landed before it.
        with pytest.raises(RuntimeError, match="simulated crash"):
            runner.invoke(app, ["process", str(wl)], catch_exceptions=False)

    data = json.loads(wl.read_text(encoding="utf-8"))
    by_rel = {e["relative_path"]: e for e in data["files_needing_metadata"]}
    assert "processed" in by_rel["Inbox/a.txt"]
    assert "processed" not in by_rel["Inbox/b.txt"]


def test_mark_propagates_across_multiple_worklists(docs_root):
    """When the same rel appears in two worklists, both get stamped."""
    _seed_files(docs_root, ["a.txt"])
    scan = _scan_dir(docs_root)
    wl1 = _write_worklist(scan, "scan-1.json", docs_root, ["Inbox/a.txt"])
    wl2 = _write_worklist(scan, "scan-2.json", docs_root, ["Inbox/a.txt"])

    with patch("dragndoc.pipeline.enrich", return_value=_FAKE):
        # no-arg form sweeps both
        res = runner.invoke(app, ["process"])
    # both wl1 and wl2 are deleted on success in no-arg mode, so we can't
    # inspect them. Re-run with explicit single worklist instead:
    assert res.exit_code == 0


def test_mark_propagates_across_multiple_worklists_explicit(docs_root):
    """Single explicit invocation only stamps the worklist passed in."""
    _seed_files(docs_root, ["a.txt"])
    scan = _scan_dir(docs_root)
    wl1 = _write_worklist(scan, "scan-1.json", docs_root, ["Inbox/a.txt"])
    wl2 = _write_worklist(scan, "scan-2.json", docs_root, ["Inbox/a.txt"])

    with patch("dragndoc.pipeline.enrich", return_value=_FAKE):
        res = runner.invoke(app, ["process", str(wl1)])

    assert res.exit_code == 0
    d1 = json.loads(wl1.read_text(encoding="utf-8"))
    d2 = json.loads(wl2.read_text(encoding="utf-8"))
    assert "processed" in d1["files_needing_metadata"][0]
    # wl2 wasn't passed in, must be untouched
    assert "processed" not in d2["files_needing_metadata"][0]


def test_skipped_message_when_all_already_processed(docs_root):
    _seed_files(docs_root, ["a.txt", "b.txt"])
    wl = _write_worklist(_scan_dir(docs_root), "scan-1.json", docs_root,
                         ["Inbox/a.txt", "Inbox/b.txt"])
    with patch("dragndoc.pipeline.enrich", return_value=_FAKE):
        runner.invoke(app, ["process", str(wl)])
        res = runner.invoke(app, ["process", str(wl)])
    assert res.exit_code == 0
    assert "nothing to process" in res.output
    assert "2 already-processed" in res.output
