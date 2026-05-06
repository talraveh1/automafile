"""Typer-based CLI for Drag'n'Doc."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Optional

import typer

from dragndoc import __version__
from dragndoc.log import get_logger


log = get_logger(__name__)

HELP_CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"]}


app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    context_settings=HELP_CONTEXT_SETTINGS,
    help="Drag'n'Doc — watch a folder, enrich files with metadata, file them via Claude.",
)
watch_app = typer.Typer(
    add_completion=False,
    invoke_without_command=True,
    context_settings=HELP_CONTEXT_SETTINGS,
    help="Control the watcher.",
)
review_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    context_settings=HELP_CONTEXT_SETTINGS,
    help="Walk metadata that needs human attention.",
)
meta_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    context_settings=HELP_CONTEXT_SETTINGS,
    help="Inspect and edit document metadata rows.",
)
toaster_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    context_settings=HELP_CONTEXT_SETTINGS,
    help="Control the Windows toaster (run on the host).",
)
triage_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    context_settings=HELP_CONTEXT_SETTINGS,
    help="Inspect / drain the triage queue (filled by digest, drained by /triage).",
)
app.add_typer(watch_app, name="watch")
app.add_typer(review_app, name="review")
app.add_typer(meta_app, name="meta")
app.add_typer(toaster_app, name="toaster")
app.add_typer(triage_app, name="triage")


def _maybe_override_docs(docs: Path | None) -> None:
    if docs is None:
        return
    os.environ["DOCS"] = str(docs.resolve())
    from dragndoc.config import reset_settings

    reset_settings()


def _run_watch_foreground(docs: Path | None) -> None:
    _maybe_override_docs(docs)
    log.info("CLI: watch (docs=%s)", docs)
    from dragndoc.watcher import run_watcher

    run_watcher()


def _request_watch_stop(*, wait: bool, timeout: float) -> None:
    log.info("CLI: watch stop (wait=%s timeout=%s)", wait, timeout)
    from dragndoc.runtime import request_stop, wait_for_running

    request_stop()
    if wait and not wait_for_running(False, timeout=timeout):
        typer.echo("watcher stop request sent, but it did not stop before the timeout", err=True)
        raise typer.Exit(1)
    typer.echo("watcher stop requested")


def _request_watch_start(*, fg: bool, docs: Path | None, wait: bool, timeout: float) -> None:
    from dragndoc.runtime import request_start, status_snapshot, wait_for_running

    if fg:
        snapshot = status_snapshot()
        if bool(snapshot["running"]):
            typer.echo("supervised watcher is already running; stop it first or use the existing background watcher", err=True)
            raise typer.Exit(1)
        log.info("CLI: watch start --fg (docs=%s)", docs)
        _run_watch_foreground(docs)
        return

    if docs is not None:
        typer.echo("--docs is only supported with --fg", err=True)
        raise typer.Exit(2)

    snapshot = status_snapshot()
    if bool(snapshot["running"]):
        pid = snapshot["pid"]
        typer.echo(f"watcher already running (pid={pid})")
        return

    log.info("CLI: watch start (wait=%s timeout=%s)", wait, timeout)
    request_start()
    if wait and not wait_for_running(True, timeout=timeout):
        typer.echo(
            "background watcher start was requested, but no supervisor started it before the timeout; "
            "use `dnd watch supervise` or `dnd watch start --fg`",
            err=True,
        )
        raise typer.Exit(1)
    typer.echo("watcher start requested")


def _show_watch_status() -> None:
    log.info("CLI: watch status")
    from dragndoc.runtime import status_snapshot

    snapshot = status_snapshot()
    state = snapshot["state"]
    pid = snapshot["pid"]
    if pid is None:
        typer.echo(f"watcher: {state}")
        return
    typer.echo(f"watcher: {state} (pid={pid})")


@app.callback()
def _root(version: Annotated[bool, typer.Option("--version", help="Print version and exit.")] = False) -> None:
    if version:
        typer.echo(f"dragndoc {__version__}")
        raise typer.Exit(0)


@watch_app.callback()
def watch(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is not None:
        return
    typer.echo(ctx.get_help(), nl=False)
    raise typer.Exit(0)


@watch_app.command("supervise")
def watch_supervise() -> None:
    """Run the container supervisor that owns the watcher process."""
    log.info("CLI: watch supervise")
    from dragndoc.runtime import supervise as supervise_runtime

    raise typer.Exit(supervise_runtime())


@watch_app.command("start")
def watch_start(
    fg: Annotated[bool, typer.Option("--fg", help="Run the watcher in the foreground instead of resuming the supervised background watcher.")] = False,
    docs: Annotated[Optional[Path], typer.Option("--docs", help="Override DOCS when using --fg.")] = None,
    wait: Annotated[bool, typer.Option("--wait/--no-wait", help="Wait until the background watcher is running.")] = True,
    timeout: Annotated[float, typer.Option("--timeout", min=0.1, help="Max seconds to wait when --wait is set.")] = 10.0,
) -> None:
    """Start or resume the watcher."""
    _request_watch_start(fg=fg, docs=docs, wait=wait, timeout=timeout)


@watch_app.command("stop")
def watch_stop(
    wait: Annotated[bool, typer.Option("--wait/--no-wait", help="Wait until the watcher has stopped.")] = True,
    timeout: Annotated[float, typer.Option("--timeout", min=0.1, help="Max seconds to wait when --wait is set.")] = 10.0,
) -> None:
    """Stop the supervised watcher without exiting the container."""
    _request_watch_stop(wait=wait, timeout=timeout)


@watch_app.command("status")
def watch_status() -> None:
    """Show whether the supervised watcher is running, stopped, or idle."""
    _show_watch_status()


# ---------------------------------------------------------------------------
# process / scan / ocr
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# review group
# ---------------------------------------------------------------------------


@review_app.command("ocr")
def review_ocr(
    yes_all: Annotated[bool, typer.Option("--yes-all", help="Re-OCR every candidate without asking.")] = False,
) -> None:
    """Walk OCR review candidates (engine/lang drift since extraction)."""
    from dragndoc.config import get_settings
    from dragndoc.meta_store import OcrInfo, get_by_path, upsert, utc_now_iso
    from dragndoc.ocr import run_ocr, tesseract_version
    from dragndoc.scanner import run_scan

    log.info("CLI: review ocr (yes_all=%s)", yes_all)
    settings = get_settings()
    wl = run_scan()
    if not wl.ocr_review_candidates:
        typer.echo("No OCR review candidates.")
        return

    for entry in wl.ocr_review_candidates:
        rel = entry["relative_path"]
        full = settings.docs / rel
        typer.echo(f"\nCandidate: {rel}")
        typer.echo(f"  previous: {entry.get('previous_engine')} / {entry.get('previous_languages')}")
        typer.echo(f"  current : {entry.get('current_engine')} / {entry.get('current_languages')}")
        choice = "y" if yes_all else typer.prompt("Re-OCR? [y/N/skip]", default="N")
        if choice.lower().startswith("y"):
            try:
                _ = run_ocr(full)
                doc = get_by_path(rel)
                if doc is not None:
                    doc.ocr = OcrInfo(
                        decision="ocr_full",
                        done=utc_now_iso(),
                        engine="tesseract",
                        engine_ver=tesseract_version(),
                        langs=[s.strip() for s in settings.tesseract.langs.replace("+", ",").split(",") if s.strip()],
                    )
                    upsert(doc)
                typer.echo("  re-OCR'd.")
            except Exception as exc:  # noqa: BLE001
                typer.echo(f"  failed: {exc}")
        elif choice.lower().startswith("s"):
            typer.echo("  skipped (will reappear on next scan)")
        else:
            typer.echo("  declined.")


@review_app.command("orphans")
def review_orphans(
    yes_all: Annotated[bool, typer.Option("--yes-all", help="Auto-accept hash-matched relinks when there is a single match.")] = False,
) -> None:
    """Walk rows whose file is missing on disk; offer hash-matched relinks."""
    from dragndoc.metadata.reconcile import find_orphans, relink

    log.info("CLI: review orphans (yes_all=%s)", yes_all)
    orphans = find_orphans()
    if not orphans:
        typer.echo("No orphan rows.")
        return

    for orphan in orphans:
        typer.echo(f"\nOrphan row id={orphan.doc_id}")
        typer.echo(f"  recorded path: {orphan.recorded_path}")
        if not orphan.matches_in_tree:
            typer.echo("  no hash matches; leave for manual cleanup.")
            continue
        for i, p in enumerate(orphan.matches_in_tree):
            typer.echo(f"  [{i}] {p}")
        if yes_all and len(orphan.matches_in_tree) == 1:
            choice = "0"
        else:
            choice = typer.prompt("Pick index to relink (or 'n' to skip)", default="n")
        if choice.lower().startswith("n"):
            continue
        try:
            idx = int(choice)
            target = orphan.matches_in_tree[idx]
        except (ValueError, IndexError):
            typer.echo("  invalid choice; skipping.")
            continue
        relink(orphan.doc_id, target)
        typer.echo(f"  relinked to {target}")


# ---------------------------------------------------------------------------
# meta group
# ---------------------------------------------------------------------------


_META_FRONTMATTER_FIELDS = {
    "category", "parties", "langs", "tags", "date", "title", "confidence", "summary", "notes",
}


@meta_app.command("get")
def meta_get(
    path: Annotated[Path, typer.Argument(help="File path. Looks up the row by relative path under the docs root.")],
) -> None:
    """JSON dump of one row (was `inspect`)."""
    from dragndoc.meta_store import get_by_file

    doc = get_by_file(path)
    if doc is None:
        typer.echo(f"no row for: {path}", err=True)
        raise typer.Exit(1)
    payload = asdict(doc)  # pyright: ignore[reportArgumentType]
    typer.echo(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


@meta_app.command("cat")
def meta_cat(
    path: Annotated[Path, typer.Argument(help="File path. Renders the row as markdown + frontmatter.")],
) -> None:
    """Markdown render of one row (frontmatter + Summary + Notes)."""
    from dragndoc.meta_store import get_by_file, to_markdown

    doc = get_by_file(path)
    if doc is None:
        typer.echo(f"no row for: {path}", err=True)
        raise typer.Exit(1)
    typer.echo(to_markdown(doc), nl=False)


@meta_app.command("set")
def meta_set(
    path: Annotated[Path, typer.Argument(help="File path.")],
    assignments: Annotated[list[str], typer.Argument(help="One or more `field=value` pairs.")],
) -> None:
    """Set one or more fields on a row. ``field=value``; lists comma-separated (e.g. `tags=tax,2025`)."""
    from dragndoc.meta_store import get_by_file, upsert

    doc = get_by_file(path)
    if doc is None:
        typer.echo(f"no row for: {path}", err=True)
        raise typer.Exit(1)

    for assignment in assignments:
        if "=" not in assignment:
            typer.echo(f"bad assignment (expected field=value): {assignment}", err=True)
            raise typer.Exit(2)
        key, _, value = assignment.partition("=")
        key = key.strip()
        value = value.strip()
        if key not in _META_FRONTMATTER_FIELDS:
            typer.echo(f"unknown or read-only field: {key}", err=True)
            raise typer.Exit(2)
        if key in {"parties", "langs", "tags"}:
            setattr(doc, key, [v.strip() for v in value.split(",") if v.strip()])
        elif key == "summary":
            doc.summary = value
        elif key == "notes":
            doc.notes = value
        else:
            setattr(doc, key, value or None)
    upsert(doc)
    typer.echo(f"updated: {doc.path}")


@meta_app.command("apply")
def meta_apply(
    path: Annotated[Path, typer.Argument(help="File path of the document.")],
    source: Annotated[Path, typer.Argument(help="Markdown file with YAML frontmatter to apply.")],
) -> None:
    """Whole-doc update from a markdown + frontmatter file."""
    from dragndoc.meta_store import doc_from_markdown, get_by_file, upsert

    base = get_by_file(path)
    if base is None:
        typer.echo(f"no row for: {path}", err=True)
        raise typer.Exit(1)
    if not source.exists():
        typer.echo(f"source file not found: {source}", err=True)
        raise typer.Exit(1)

    text = source.read_text(encoding="utf-8")
    try:
        new_doc = doc_from_markdown(text, base=base)
    except ValueError as exc:
        typer.echo(f"could not parse: {exc}", err=True)
        raise typer.Exit(2) from None
    new_doc.path = base.path
    new_doc.hash = base.hash
    new_doc.size = base.size
    new_doc.original = base.original
    upsert(new_doc)
    typer.echo(f"applied: {new_doc.path}")


@meta_app.command("edit")
def meta_edit(
    path: Annotated[Path, typer.Argument(help="File path of the document.")],
) -> None:
    """Open the row's markdown in $EDITOR; apply on save."""
    from dragndoc.meta_store import doc_from_markdown, get_by_file, to_markdown, upsert

    doc = get_by_file(path)
    if doc is None:
        typer.echo(f"no row for: {path}", err=True)
        raise typer.Exit(1)

    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or ("notepad" if sys.platform == "win32" else "vi")
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
        f.write(to_markdown(doc))
        tmp_name = f.name

    try:
        subprocess.run([editor, tmp_name], check=False)
        edited = Path(tmp_name).read_text(encoding="utf-8")
        try:
            new_doc = doc_from_markdown(edited, base=doc)
        except ValueError as exc:
            typer.echo(f"could not parse edited file (left at {tmp_name}): {exc}", err=True)
            raise typer.Exit(2) from None
        new_doc.path = doc.path
        new_doc.hash = doc.hash
        new_doc.size = doc.size
        new_doc.original = doc.original
        upsert(new_doc)
        typer.echo(f"applied: {new_doc.path}")
    finally:
        try:
            Path(tmp_name).unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# grep
# ---------------------------------------------------------------------------


_FTS_FIELDS = ("title", "summary", "notes", "tags", "parties")


@app.command()
def grep(
    pattern: Annotated[str, typer.Argument(help="FTS5 query: a word, phrase, or boolean expression (e.g. `tax AND receipt`).")],
    field: Annotated[Optional[str], typer.Option("--field", help=f"Restrict to one column: {', '.join(_FTS_FIELDS)}.")] = None,
    limit: Annotated[int, typer.Option("--limit", help="Max rows to return.")] = 50,
) -> None:
    """Search metadata with FTS5."""
    from dragndoc.db import connect

    if field is not None and field not in _FTS_FIELDS:
        typer.echo(f"--field must be one of: {', '.join(_FTS_FIELDS)}", err=True)
        raise typer.Exit(2)

    if field:
        # FTS5 column scoping: prefix each query term with `colname:` would be
        # tedious for a free-form `pattern`; the `{col} : query` form scopes
        # the entire query to that column.
        match_query = f"{{{field}}} : {pattern}"
    else:
        match_query = pattern

    with connect(readonly=True) as conn:
        rows = conn.execute(
            "SELECT d.path, d.title, d.category "
            "FROM docs d JOIN docs_fts f ON d.id = f.rowid "
            "WHERE docs_fts MATCH ? "
            "ORDER BY bm25(docs_fts) "
            "LIMIT ?",
            (match_query, limit),
        ).fetchall()

    if not rows:
        typer.echo("(no matches)")
        return
    for r in rows:
        title = r["title"] or ""
        suffix = f" — {title}" if title else ""
        typer.echo(f"{r['path']} [{r['category']}]{suffix}")


# ---------------------------------------------------------------------------
# file ops: mv / cp / rm / ls
# ---------------------------------------------------------------------------


@app.command()
def mv(
    src: Annotated[Path, typer.Argument(help="Source file path.")],
    dst: Annotated[Path, typer.Argument(help="Destination file path or directory.")],
    force: Annotated[bool, typer.Option("-f", "--force", help="Overwrite the target file if it exists.")] = False,
) -> None:
    """Move a file. Updates the metadata row's path."""
    import shutil as _shutil
    from dragndoc.db import transaction
    from dragndoc.meta_store import relative_to_root

    if not src.exists():
        typer.echo(f"src not found: {src}", err=True)
        raise typer.Exit(1)
    if not src.is_file():
        typer.echo(f"src is not a file: {src}", err=True)
        raise typer.Exit(1)

    target = dst / src.name if dst.exists() and dst.is_dir() else dst
    if target.resolve() == src.resolve():
        typer.echo(f"src and dst are the same: {src}", err=True)
        raise typer.Exit(1)
    if target.exists() and not force:
        typer.echo(f"target exists: {target} (use -f to overwrite)", err=True)
        raise typer.Exit(1)

    target.parent.mkdir(parents=True, exist_ok=True)
    log.info("CLI: mv %s -> %s (force=%s)", src, target, force)
    src_rel = relative_to_root(src)
    _shutil.move(str(src), str(target))
    new_rel = relative_to_root(target)
    with transaction() as conn:
        conn.execute("UPDATE docs SET path = ? WHERE path = ?", (new_rel, src_rel))
    typer.echo(f"moved: {target}")


@app.command()
def cp(
    src: Annotated[Path, typer.Argument(help="Source file path.")],
    dst: Annotated[Path, typer.Argument(help="Destination file path or directory.")],
    force: Annotated[bool, typer.Option("-f", "--force", help="Overwrite the target file if it exists.")] = False,
) -> None:
    """Copy a file. Duplicates the metadata row at the new path (same hash)."""
    import shutil as _shutil
    from dragndoc.meta_store import get_by_file, relative_to_root, upsert

    if not src.exists():
        typer.echo(f"src not found: {src}", err=True)
        raise typer.Exit(1)
    if not src.is_file():
        typer.echo(f"src is not a file: {src}", err=True)
        raise typer.Exit(1)

    target = dst / src.name if dst.exists() and dst.is_dir() else dst
    if target.resolve() == src.resolve():
        typer.echo(f"src and dst are the same: {src}", err=True)
        raise typer.Exit(1)
    if target.exists() and not force:
        typer.echo(f"target exists: {target} (use -f to overwrite)", err=True)
        raise typer.Exit(1)

    target.parent.mkdir(parents=True, exist_ok=True)
    log.info("CLI: cp %s -> %s (force=%s)", src, target, force)
    _shutil.copy2(str(src), str(target))
    src_doc = get_by_file(src)
    if src_doc is not None:
        src_doc.id = None  # force INSERT path on upsert
        src_doc.path = relative_to_root(target)
        upsert(src_doc)
    typer.echo(f"copied: {target}")


@app.command()
def rm(
    path: Annotated[Path, typer.Argument(help="File path to remove.")],
    force: Annotated[bool, typer.Option("-f", "--force", help="Ignore missing file and exit successfully.")] = False,
) -> None:
    """Remove a file and its metadata row."""
    from dragndoc.meta_store import delete_by_path, relative_to_root

    if not path.exists():
        if force:
            return
        typer.echo(f"not found: {path}", err=True)
        raise typer.Exit(1)
    if not path.is_file():
        typer.echo(f"not a file: {path}", err=True)
        raise typer.Exit(1)

    log.info("CLI: rm %s (force=%s)", path, force)
    rel = relative_to_root(path)
    path.unlink()
    delete_by_path(rel)
    typer.echo(f"removed: {path}")


@app.command()
def ls(
    path: Annotated[Path, typer.Argument(help="Directory to list.")] = Path("."),
    show_all: Annotated[bool, typer.Option("-a", "--all", help="Show entries that start with a dot.")] = False,
) -> None:
    """List a directory; files that have a metadata row are marked with ``*``."""
    from dragndoc.db import connect
    from dragndoc.meta_store import relative_to_root

    if not path.exists():
        typer.echo(f"not found: {path}", err=True)
        raise typer.Exit(1)
    if not path.is_dir():
        typer.echo(f"not a directory: {path}", err=True)
        raise typer.Exit(1)

    entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    files_for_query = [e for e in entries if e.is_file() and (show_all or not e.name.startswith("."))]
    rels = [relative_to_root(f) for f in files_for_query]
    rows: set[str] = set()
    if rels:
        placeholders = ",".join("?" * len(rels))
        with connect(readonly=True) as conn:
            for r in conn.execute(
                f"SELECT path FROM docs WHERE path IN ({placeholders})", rels
            ).fetchall():
                rows.add(r["path"])

    rel_iter = iter(rels)
    for entry in entries:
        if not show_all and entry.name.startswith("."):
            continue
        if entry.is_dir():
            typer.echo(f"  {entry.name}/")
        else:
            rel = next(rel_iter)
            mark = "*" if rel in rows else " "
            typer.echo(f"{mark} {entry.name}")


# ---------------------------------------------------------------------------
# top-level utilities
# ---------------------------------------------------------------------------


@app.command()
def bootstrap(
    force: Annotated[bool, typer.Option("--force", help="Overwrite memory templates even if present.")] = False,
) -> None:
    """Seed memory templates, create the data folder, and bootstrap the DB schema. Idempotent."""
    log.info("CLI: bootstrap (force=%s)", force)
    from dragndoc.bootstrap import bootstrap as bootstrap_fn
    bootstrap_fn(force=force)


@app.command()
def doctor() -> None:
    """Diagnose the local environment (Tesseract, Ollama, paths)."""
    log.info("CLI: doctor")
    from dragndoc.config import get_settings
    from dragndoc.llm import ollama_available, ollama_has_model
    from dragndoc.ocr import tesseract_available, tesseract_languages, tesseract_version

    settings = get_settings()
    typer.echo(f"Docs root: {settings.docs}{'  (exists)' if settings.docs.exists() else '  (missing)'}")
    typer.echo(f"  inbox: {settings.inbox_path}{'  (exists)' if settings.inbox_path.exists() else '  (missing)'}")
    typer.echo(f"Data dir : {settings.data_dir}{'  (exists)' if settings.data_dir.exists() else '  (missing)'}")
    typer.echo(f"  db     : {settings.db_path}{'  (exists)' if settings.db_path.exists() else '  (missing)'}")

    tess = tesseract_available()
    typer.echo(f"Tesseract present: {tess}")
    if tess:
        typer.echo(f"  version  : {tesseract_version()}")
        typer.echo(f"  langs    : {', '.join(tesseract_languages()) or '(unknown)'}")

    available = ollama_available()
    typer.echo(f"Ollama reachable : {available}  ({settings.ollama.url})")
    if available:
        typer.echo(f"  model present : {ollama_has_model()}  ({settings.ollama.model})")


@toaster_app.command("start")
def toaster_start(
    fg: Annotated[bool, typer.Option("--fg", help="Run the toaster in this process instead of spawning a detached one.")] = False,
    no_tray: Annotated[bool, typer.Option("--no-tray", help="Run headless (no tray icon). For debugging or pipes.")] = False,
) -> None:
    """Start the toaster (background by default; ``--fg`` to run in this terminal)."""
    log.info("CLI: toaster start (fg=%s no_tray=%s)", fg, no_tray)
    from dragndoc.toaster import start_background, start_foreground

    if fg:
        raise typer.Exit(start_foreground(tray=not no_tray))
    raise typer.Exit(start_background(tray=not no_tray))


@toaster_app.command("stop")
def toaster_stop(
    timeout: Annotated[float, typer.Option("--timeout", min=0.1, help="Max seconds to wait for the toaster to exit.")] = 10.0,
) -> None:
    """Stop the running toaster."""
    log.info("CLI: toaster stop (timeout=%s)", timeout)
    from dragndoc.toaster import stop_toaster
    raise typer.Exit(stop_toaster(timeout=timeout))


@toaster_app.command("restart")
def toaster_restart(
    timeout: Annotated[float, typer.Option("--timeout", min=0.1, help="Max seconds to wait for the toaster to exit before relaunching.")] = 10.0,
    no_tray: Annotated[bool, typer.Option("--no-tray", help="Run headless (no tray icon). For debugging or pipes.")] = False,
) -> None:
    """Restart the running background toaster."""
    log.info("CLI: toaster restart (timeout=%s no_tray=%s)", timeout, no_tray)
    from dragndoc.toaster import restart_toaster
    raise typer.Exit(restart_toaster(tray=not no_tray, timeout=timeout))


@toaster_app.command("status")
def toaster_status() -> None:
    """Show whether the toaster is running, plus install state (shortcut + AUMID)."""
    log.info("CLI: toaster status")
    from dragndoc.toaster import status_snapshot

    snapshot = status_snapshot()
    state = snapshot["state"]
    pid = snapshot["pid"]
    if pid is None:
        typer.echo(f"toaster: {state}")
    else:
        typer.echo(f"toaster: {state} (pid={pid})")

    if sys.platform == "win32":
        from dragndoc.toaster_setup import status as setup_status
        setup_status()


@toaster_app.command("install")
def toaster_install() -> None:
    """Install the Windows Startup shortcut + register the AUMID."""
    if sys.platform != "win32":
        typer.echo("install is Windows-only", err=True)
        raise typer.Exit(2)
    log.info("CLI: toaster install")
    from dragndoc.toaster_setup import install
    raise typer.Exit(install())


@toaster_app.command("uninstall")
def toaster_uninstall() -> None:
    """Remove the Windows Startup shortcut + unregister the AUMID."""
    if sys.platform != "win32":
        typer.echo("uninstall is Windows-only", err=True)
        raise typer.Exit(2)
    log.info("CLI: toaster uninstall")
    from dragndoc.toaster_setup import uninstall
    raise typer.Exit(uninstall())


# ---------------------------------------------------------------------------
# triage queue
# ---------------------------------------------------------------------------


def _triage_entry_to_dict(entry) -> dict:
    """Flatten a QueueEntry to a JSON-friendly dict including the doc fields."""
    d = asdict(entry.doc)  # pyright: ignore[reportArgumentType]
    return {
        "doc_id": entry.doc.id,
        "path": entry.doc.path,
        "category": entry.doc.category,
        "title": entry.doc.title,
        "summary": entry.doc.summary,
        "confidence": entry.doc.confidence,
        "enqueued_at": entry.enqueued_at,
        "reason": entry.reason,
        "doc": d,
    }


@triage_app.command("count")
def triage_count_cmd(
    all_: Annotated[bool, typer.Option("--all", help="Count everything in the queue, not just inbox files.")] = False,
) -> None:
    """Print the number of files awaiting triage."""
    from dragndoc.triage_queue import count as q_count

    typer.echo(str(q_count(inbox_only=not all_)))


@triage_app.command("list")
def triage_list_cmd(
    all_: Annotated[bool, typer.Option("--all", help="Show everything queued, not just inbox files.")] = False,
    print_json: Annotated[bool, typer.Option("--json", help="Print full queue as JSON.")] = False,
) -> None:
    """List files awaiting triage, oldest first."""
    from dragndoc.triage_queue import list_queue

    entries = list_queue(inbox_only=not all_)
    if print_json:
        typer.echo(json.dumps([_triage_entry_to_dict(e) for e in entries], indent=2, ensure_ascii=False, default=str))
        return
    if not entries:
        typer.echo("(queue empty)")
        return
    for e in entries:
        typer.echo(f"{e.enqueued_at}  {e.doc.path}  [{e.doc.category}]  {e.reason}")


@triage_app.command("next")
def triage_next_cmd(
    all_: Annotated[bool, typer.Option("--all", help="Don't restrict to inbox; pull the oldest entry from anywhere.")] = False,
    print_json: Annotated[bool, typer.Option("--json/--no-json", help="Print full doc + queue metadata as JSON (default). Use --no-json for a one-line summary.")] = True,
) -> None:
    """Peek at the next item to triage. Does NOT remove it; call `dnd triage done` after filing."""
    from dragndoc.triage_queue import next_entry

    entry = next_entry(inbox_only=not all_)
    if entry is None:
        if print_json:
            typer.echo("null")
        else:
            typer.echo("(queue empty)")
        raise typer.Exit(1)
    if print_json:
        typer.echo(json.dumps(_triage_entry_to_dict(entry), indent=2, ensure_ascii=False, default=str))
    else:
        typer.echo(f"{entry.doc.path}  [{entry.doc.category}]  enqueued={entry.enqueued_at}  reason={entry.reason}")


@triage_app.command("done")
def triage_done_cmd(
    path: Annotated[Path, typer.Argument(help="File path to remove from the queue.")],
) -> None:
    """Remove a file from the triage queue (call after filing it)."""
    from dragndoc.meta_store import relative_to_root
    from dragndoc.triage_queue import dequeue_by_path

    rel = relative_to_root(path)
    removed = dequeue_by_path(rel)
    if removed:
        typer.echo(f"removed: {rel}")
    else:
        typer.echo(f"not in queue: {rel}", err=True)
        raise typer.Exit(1)


@triage_app.command("rebuild")
def triage_rebuild_cmd(
    all_: Annotated[bool, typer.Option("--all", help="Seed from every doc, not just inbox files.")] = False,
) -> None:
    """Seed the queue from existing docs that aren't already queued. One-shot migration aid."""
    from dragndoc.triage_queue import rebuild_from_existing_docs

    n = rebuild_from_existing_docs(inbox_only=not all_)
    typer.echo(f"enqueued: {n}")


@triage_app.command("clear")
def triage_clear_cmd(
    all_: Annotated[bool, typer.Option("--all", help="Empty the entire queue, not just inbox entries.")] = False,
    yes: Annotated[bool, typer.Option("-y", "--yes", help="Skip confirmation prompt.")] = False,
) -> None:
    """Empty the triage queue (default scope: inbox only)."""
    from dragndoc.triage_queue import clear, count as q_count

    n = q_count(inbox_only=not all_)
    if n == 0:
        typer.echo("(queue empty)")
        return
    if not yes:
        scope = "all queued" if all_ else "queued inbox"
        confirm = typer.prompt(f"Remove {n} {scope} entries? [y/N]", default="N")
        if not confirm.lower().startswith("y"):
            typer.echo("aborted")
            raise typer.Exit(1)
    removed = clear(inbox_only=not all_)
    typer.echo(f"removed: {removed}")


def main() -> None:  # pragma: no cover
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
