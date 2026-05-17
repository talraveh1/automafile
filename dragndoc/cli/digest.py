"""`dnd digest`, `dnd scan`, `dnd ocr` — root-level pipeline commands."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Annotated, Optional

import typer

from dragndoc.cli import app
from dragndoc.log import get_logger


log = get_logger(__name__)


@app.command()
def digest(
    paths: Annotated[Optional[list[Path]], typer.Argument(
        help="One or more files, directories, or glob patterns (e.g. 'Inbox/voix/**/*.mp3'). "
             "Omit to scan the whole tree and digest anything that needs it."
    )] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Run extraction + LLM but write nothing.")] = False,
    force_ocr: Annotated[bool, typer.Option("--force-ocr", help="Force OCR even if not recommended.")] = False,
    force: Annotated[bool, typer.Option("-f", "--force", help="Re-digest even if the row's `digested` mark is at-or-after the file's mtime.")] = False,
    recursive: Annotated[bool, typer.Option("-r", "--recursive", help="When an argument is a directory, walk its whole subtree.")] = False,
    insensitive: Annotated[bool, typer.Option("-i", "--insensitive", help="Case-insensitive glob matching.")] = False,
    stop_on_error: Annotated[bool, typer.Option("--stop-on-error", help="Stop at the first failure when digesting many files.")] = False,
) -> None:
    """Digest specific files, directories, or globs — or, with no args, the whole tree."""
    from dragndoc.config import get_settings
    from dragndoc.events import DIGEST_FINISHED, DIGEST_STARTED, append as append_event
    from dragndoc.pipeline import digest_file, format_result_line
    from dragndoc.triage import count as triage_count
    from dragndoc.cli._path_args import expand_paths

    settings = get_settings()

    if not paths:
        # whole-tree behavior: scan + digest everything that needs it
        _digest_tree(settings, dry_run=dry_run, force_ocr=force_ocr, force=force, stop_on_error=stop_on_error)
        return

    # expand paths: files / dirs / globs → concrete file list
    expanded = expand_paths(paths, recursive=recursive, insensitive=insensitive)
    if not expanded:
        typer.echo(f"No matching files: {', '.join(str(p) for p in paths)}", err=True)
        raise typer.Exit(1)

    if len(expanded) == 1 and (paths and not _arg_is_multi_input(paths[0])):
        # one positional arg, one resolved file → preserve the legacy single-file path
        _digest_single_file(
            expanded[0],
            dry_run=dry_run,
            force_ocr=force_ocr,
            triage_count_fn=triage_count,
            append_event_fn=append_event,
            DIGEST_STARTED=DIGEST_STARTED,
            DIGEST_FINISHED=DIGEST_FINISHED,
        )
        return

    log.info("CLI: digest %d file(s) (dry_run=%s force_ocr=%s force=%s recursive=%s insensitive=%s)",
             len(expanded), dry_run, force_ocr, force, recursive, insensitive)
    typer.echo(f"Digesting {len(expanded)} file(s)")
    append_event(DIGEST_STARTED, scope="batch", count=len(expanded))

    failures = 0
    try:
        for fp in expanded:
            try:
                result = digest_file(fp, dry_run=dry_run, force_ocr=force_ocr)
            except Exception as exc:  # noqa: BLE001
                msg = f"{fp.name} | CRASH: {exc}"
                typer.echo(msg)
                log.error(msg)
                failures += 1
                if stop_on_error:
                    raise typer.Exit(1) from None
                continue
            line = format_result_line(result)
            typer.echo(line)
            if result.error:
                log.error("%s", line)
                failures += 1
                if stop_on_error:
                    raise typer.Exit(1)
    finally:
        append_event(
            DIGEST_FINISHED,
            scope="batch",
            succeeded=len(expanded) - failures,
            failed=failures,
            ready_count=triage_count(),
        )

    typer.echo(f"Done: {len(expanded) - failures}/{len(expanded)} succeeded")
    if failures:
        raise typer.Exit(1)


def _arg_is_multi_input(arg: Path) -> bool:
    """True when the user gave a glob pattern or a directory (likely multi-file)."""
    from dragndoc.cli._path_args import is_pattern_arg
    return is_pattern_arg(arg)


def _digest_single_file(
    path: Path,
    *,
    dry_run: bool,
    force_ocr: bool,
    triage_count_fn,
    append_event_fn,
    DIGEST_STARTED: str,
    DIGEST_FINISHED: str,
) -> None:
    """The original single-file digest path, preserved for back-compat."""
    from dragndoc.meta_store import get_by_file, recompute_dups_for_hashes
    from dragndoc.metadata.hashing import hash_file
    from dragndoc.pipeline import digest_file, format_result_line

    log.info("CLI: digest %s (dry_run=%s force_ocr=%s)", path, dry_run, force_ocr)
    append_event_fn(DIGEST_STARTED, scope="file", file=path.name)
    old = get_by_file(path)
    old_hash = old.hash if old else None
    file_hash = hash_file(path) if path.exists() and path.is_file() else None
    result = digest_file(path, dry_run=dry_run, force_ocr=force_ocr, file_hash=file_hash)
    if not dry_run and file_hash:
        recompute_dups_for_hashes({h for h in (old_hash, file_hash) if h})
    line = format_result_line(result)
    typer.echo(line)
    if result.error:
        log.error("%s", line)
    append_event_fn(
        DIGEST_FINISHED,
        scope="file",
        file=path.name,
        succeeded=0 if result.error else 1,
        failed=1 if result.error else 0,
        category=result.category,
        ready_count=triage_count_fn(),
    )
    if result.error:
        raise typer.Exit(1)


def _digest_tree(settings, *, dry_run: bool, force_ocr: bool, force: bool, stop_on_error: bool) -> None:
    from dragndoc.events import DIGEST_FINISHED, DIGEST_STARTED, append as append_event
    from dragndoc.pipeline import digest_file, format_result_line
    from dragndoc.scanner import run_scan
    from dragndoc.triage import count as triage_count
    from dragndoc.meta_store import recompute_dups

    log.info("CLI: digest tree (dry_run=%s force_ocr=%s force=%s)", dry_run, force_ocr, force)
    report = run_scan(force=force)
    candidates = list(report.worklist.iter_digest_candidates())
    if not candidates:
        typer.echo("Nothing to digest")
        return

    typer.echo(f"Digesting {len(candidates)} file(s)")

    append_event(DIGEST_STARTED, scope="tree", count=len(candidates))

    failures = 0
    try:
        for candidate in candidates:
            full = settings.docs / candidate.rel
            if not full.exists():
                msg = f"{candidate.rel} | MISSING under {settings.docs}"
                typer.echo(msg)
                log.error("%s", msg)
                failures += 1
                if stop_on_error:
                    raise typer.Exit(1)
                continue
            # expected facts guard against digesting a file that changed after scan
            result = digest_file(
                full,
                dry_run=dry_run,
                force_ocr=force_ocr,
                file_hash=candidate.file_hash,
                expected_size=candidate.size,
                expected_mtime=candidate.mtime,
            )
            line = format_result_line(result)
            typer.echo(line)
            if result.error:
                log.error("%s", line)
                failures += 1
                if stop_on_error:
                    raise typer.Exit(1)
        if not dry_run:
            recompute_dups()
    finally:
        append_event(
            DIGEST_FINISHED,
            scope="tree",
            succeeded=len(candidates) - failures,
            failed=failures,
            ready_count=triage_count(),
        )

    typer.echo(f"Done: {len(candidates) - failures}/{len(candidates)} succeeded")
    if failures:
        raise typer.Exit(1)


@app.command()
def scan(
    docs: Annotated[Optional[Path], typer.Option("--docs", help="Override DOCS for this scan (sets the env var and resets cached settings).")] = None,
    path: Annotated[Optional[Path], typer.Option("--path", help="Limit the scan to a relative subpath under DOCS (e.g. 'Inbox').")] = None,
    print_json: Annotated[bool, typer.Option("--json", help="Print the worklist as JSON.")] = False,
) -> None:
    """Run the scanner; reconcile rows with the filesystem and report digest work."""
    if docs is not None:
        os.environ["DOCS"] = str(docs.resolve())
        from dragndoc.config import reset_settings
        reset_settings()
    log.info("CLI: scan (path=%s, json=%s)", path, print_json)
    from dragndoc.events import SCAN_FINISHED, SCAN_STARTED, append as append_event
    from dragndoc.scanner import run_scan
    from dragndoc.triage import count as triage_count

    append_event(SCAN_STARTED, scope="subpath" if path else "tree", path=str(path) if path else None)
    wl = None
    try:
        try:
            wl = run_scan(subpath=path)
        except (FileNotFoundError, ValueError) as e:
            msg = str(e) or e.__class__.__name__
            typer.echo(msg, err=True)
            log.error("%s", msg)
            raise typer.Exit(2) from None
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
        f"Scan complete: seen={wl.files_seen} need_ocr={len(wl.files_needing_ocr)} "
        f"need_asr={len(wl.files_needing_asr)} "
        f"need_meta={len(wl.files_needing_metadata)} partial={len(wl.files_with_partial_metadata)} "
        f"stale={len(wl.files_with_stale_metadata)} ocr_review={len(wl.ocr_review_candidates)} "
        f"asr_review={len(wl.asr_review_candidates)} "
        f"missing={len(wl.missing_files)} unprocessable={len(wl.unprocessable_files)}"
    )
    if (
        wl.files_needing_metadata or wl.files_needing_ocr or wl.files_needing_asr
        or wl.files_with_partial_metadata or wl.files_with_stale_metadata
    ):
        typer.echo("Next: dnd digest")


@app.command()
def ocr(
    paths: Annotated[list[Path], typer.Argument(help="One or more files, directories, or glob patterns to OCR.")],
    langs: Annotated[Optional[str], typer.Option(help="Override tesseract.langs.")] = None,
    recursive: Annotated[bool, typer.Option("-r", "--recursive", help="When an argument is a directory, walk its whole subtree.")] = False,
    insensitive: Annotated[bool, typer.Option("-i", "--insensitive", help="Case-insensitive glob matching.")] = False,
) -> None:
    """Force OCR on one or more files and print the recovered text."""
    from dragndoc.cli._path_args import expand_paths
    from dragndoc.ocr import run_ocr

    log.info("CLI: ocr %d arg(s) (langs=%s recursive=%s insensitive=%s)", len(paths), langs, recursive, insensitive)
    expanded = expand_paths(paths, recursive=recursive, insensitive=insensitive)
    if not expanded:
        typer.echo(f"No matching files: {', '.join(str(p) for p in paths)}", err=True)
        raise typer.Exit(1)

    for i, fp in enumerate(expanded):
        if len(expanded) > 1:
            if i:
                typer.echo("\n---\n")
            typer.echo(f"## {fp}\n")
        try:
            typer.echo(run_ocr(fp, langs=langs))
        except Exception as exc:  # noqa: BLE001
            typer.echo(f"OCR failed for {fp}: {exc}", err=True)
            if len(expanded) == 1:
                raise typer.Exit(1) from None
