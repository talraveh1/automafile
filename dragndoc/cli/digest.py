"""`dnd digest`, `dnd scan`, `dnd ocr` — root-level pipeline commands."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Optional

import typer

from dragndoc.cli import app
from dragndoc.log import get_logger


log = get_logger(__name__)


@app.command()
def digest(
    path: Annotated[Optional[Path], typer.Argument(help="A specific file to digest. Omit to scan the whole tree and digest anything that needs it.")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Run extraction + LLM but write nothing.")] = False,
    force_ocr: Annotated[bool, typer.Option("--force-ocr", help="Force OCR even if not recommended.")] = False,
    force: Annotated[bool, typer.Option("-f", "--force", help="Re-digest even if the row's `digested` mark is at-or-after the file's mtime.")] = False,
    stop_on_error: Annotated[bool, typer.Option("--stop-on-error", help="Stop at the first failure when digesting many files.")] = False,
) -> None:
    """Digest a single file or scan the tree and digest everything that needs it."""
    from dragndoc.config import get_settings
    from dragndoc.events import DIGEST_FINISHED, DIGEST_STARTED, append as append_event
    from dragndoc.pipeline import digest_file, format_result_line
    from dragndoc.triage_queue import count as triage_count

    settings = get_settings()
    if path is not None:
        log.info("CLI: digest %s (dry_run=%s force_ocr=%s)", path, dry_run, force_ocr)
        append_event(DIGEST_STARTED, scope="file", file=path.name)
        result = digest_file(path, dry_run=dry_run, force_ocr=force_ocr)
        typer.echo(format_result_line(result))
        append_event(
            DIGEST_FINISHED,
            scope="file",
            file=path.name,
            succeeded=0 if result.error else 1,
            failed=1 if result.error else 0,
            category=result.category,
            ready_count=triage_count(),
        )
        if result.error:
            raise typer.Exit(1)
        return

    _digest_tree(settings, dry_run=dry_run, force_ocr=force_ocr, force=force, stop_on_error=stop_on_error)


def _is_digested_fresh(rel: str, file_path: Path) -> bool:
    """True if the row's ``modified`` covers the file's current mtime."""
    from dragndoc.db import connect

    with connect(readonly=True) as conn:
        row = conn.execute(
            "SELECT digested, modified FROM docs WHERE path = ?", (rel,)
        ).fetchone()
    if row is None:
        return False
    if not row["digested"] or not row["modified"]:
        return False
    try:
        file_mt = datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc)
    except FileNotFoundError:
        return False
    try:
        recorded_mt = datetime.fromisoformat(str(row["modified"]).replace("Z", "+00:00"))
    except ValueError:
        return False
    return file_mt <= recorded_mt


def _digest_tree(settings, *, dry_run: bool, force_ocr: bool, force: bool, stop_on_error: bool) -> None:
    from dragndoc.events import DIGEST_FINISHED, DIGEST_STARTED, append as append_event
    from dragndoc.pipeline import digest_file, format_result_line
    from dragndoc.scanner import run_scan
    from dragndoc.triage_queue import count as triage_count

    log.info("CLI: digest tree (dry_run=%s force_ocr=%s force=%s)", dry_run, force_ocr, force)
    wl = run_scan()
    candidates: list[str] = []
    seen: set[str] = set()
    for bucket in (
        "files_needing_metadata",
        "files_needing_ocr",
        "files_with_partial_metadata",
        "files_with_stale_metadata",
    ):
        for entry in getattr(wl, bucket):
            rel = entry.get("relative_path")
            if rel and rel not in seen:
                seen.add(rel)
                candidates.append(rel)

    if not candidates:
        typer.echo("nothing to digest")
        return

    skipped = 0
    todo: list[str] = []
    for rel in candidates:
        full = settings.docs / rel
        if not force and _is_digested_fresh(rel, full):
            skipped += 1
            continue
        todo.append(rel)

    if not todo:
        msg = "nothing to digest"
        if skipped:
            msg += f" ({skipped} already-digested skipped; use --force to redo)"
        typer.echo(msg)
        return

    msg = f"digesting {len(todo)} file(s)"
    if skipped:
        msg += f" ({skipped} already-digested skipped; use --force to redo)"
    typer.echo(msg)

    append_event(DIGEST_STARTED, scope="tree", count=len(todo))

    failures = 0
    try:
        for rel in todo:
            full = settings.docs / rel
            if not full.exists():
                typer.echo(f"{rel} | MISSING under {settings.docs}")
                failures += 1
                if stop_on_error:
                    raise typer.Exit(1)
                continue
            result = digest_file(full, dry_run=dry_run, force_ocr=force_ocr)
            typer.echo(format_result_line(result))
            if result.error:
                failures += 1
                if stop_on_error:
                    raise typer.Exit(1)
    finally:
        append_event(
            DIGEST_FINISHED,
            scope="tree",
            succeeded=len(todo) - failures,
            failed=failures,
            ready_count=triage_count(),
        )

    typer.echo(f"done: {len(todo) - failures}/{len(todo)} succeeded")
    if failures:
        raise typer.Exit(1)


@app.command()
def scan(
    docs: Annotated[Optional[Path], typer.Option("--docs")] = None,
    path: Annotated[Optional[Path], typer.Option("--path", help="Limit the scan to a relative subpath under DOCS (e.g. 'Inbox').")] = None,
    print_json: Annotated[bool, typer.Option("--json", help="Print the worklist as JSON.")] = False,
) -> None:
    """Run the scanner; report what `digest` would do. No files are written."""
    if docs is not None:
        os.environ["DOCS"] = str(docs.resolve())
        from dragndoc.config import reset_settings
        reset_settings()
    log.info("CLI: scan (path=%s, json=%s)", path, print_json)
    from dragndoc.events import SCAN_FINISHED, SCAN_STARTED, append as append_event
    from dragndoc.scanner import run_scan
    from dragndoc.triage_queue import count as triage_count

    append_event(SCAN_STARTED, scope="subpath" if path else "tree", path=str(path) if path else None)
    wl = None
    try:
        wl = run_scan(subpath=path)
    finally:
        try:
            ready = triage_count()
        except Exception:  # noqa: BLE001
            ready = 0
        append_event(SCAN_FINISHED, seen=wl.files_seen if wl else 0, ready_count=ready)
    assert wl is not None
    if print_json:
        typer.echo(json.dumps(wl.to_dict(), indent=2, ensure_ascii=False))
        return
    typer.echo(
        f"scan complete: seen={wl.files_seen} need_ocr={len(wl.files_needing_ocr)} "
        f"need_meta={len(wl.files_needing_metadata)} partial={len(wl.files_with_partial_metadata)} "
        f"stale={len(wl.files_with_stale_metadata)} ocr_review={len(wl.ocr_review_candidates)} "
        f"missing={len(wl.missing_files)} unprocessable={len(wl.unprocessable_files)}"
    )
    if wl.files_needing_metadata or wl.files_needing_ocr or wl.files_with_partial_metadata or wl.files_with_stale_metadata:
        typer.echo("next: dnd digest")


@app.command()
def ocr(
    path: Annotated[Path, typer.Argument(help="The file to OCR.")],
    langs: Annotated[Optional[str], typer.Option(help="Override tesseract.langs.")] = None,
) -> None:
    """Force OCR on a single file and print the recovered text."""
    log.info("CLI: ocr %s (langs=%s)", path, langs)
    from dragndoc.ocr import run_ocr
    text = run_ocr(path, langs=langs)
    typer.echo(text)
