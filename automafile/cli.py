"""Typer-based CLI for Automafile."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated, Optional

import typer

from automafile import __version__
from automafile.log import get_logger


log = get_logger(__name__)


app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Automafile — watch a folder, enrich files with metadata, file them via Claude.",
)


@app.callback()
def _root(version: Annotated[bool, typer.Option("--version", help="Print version and exit.")] = False) -> None:
    if version:
        typer.echo(f"automafile {__version__}")
        raise typer.Exit(0)


@app.command()
def watch(
    documents_root: Annotated[Optional[Path], typer.Option("--documents-root", help="Override DOCUMENTS_ROOT.")] = None,
) -> None:
    """Start the inbox watcher in the foreground."""
    if documents_root is not None:
        import os
        os.environ["DOCUMENTS_ROOT"] = str(documents_root.resolve())
        from automafile.config import reset_settings
        reset_settings()
    log.info("CLI: watch (documents_root=%s)", documents_root)
    from automafile.watcher import run_watcher
    run_watcher()


@app.command()
def process(
    path: Annotated[Optional[Path], typer.Argument(help="A file to process, a worklist JSON, or omitted to auto-pick the newest matching worklist.")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Run extraction + LLM but write nothing.")] = False,
    force_ocr: Annotated[bool, typer.Option("--force-ocr", help="Force OCR even if not recommended.")] = False,
    stop_on_error: Annotated[bool, typer.Option("--stop-on-error", help="Worklist mode only: stop at the first failure.")] = False,
) -> None:
    """Process a worklist (default) or a single file."""
    from automafile.config import get_settings
    from automafile.pipeline import format_result_line, process_file

    settings = get_settings()
    if path is None or _looks_like_worklist(path, settings.scan_dir):
        _process_worklist(path, settings, dry_run=dry_run, force_ocr=force_ocr, stop_on_error=stop_on_error)
        return
    log.info("CLI: process %s (dry_run=%s force_ocr=%s)", path, dry_run, force_ocr)
    result = process_file(path, dry_run=dry_run, force_ocr=force_ocr)
    typer.echo(format_result_line(result))
    if result.error:
        raise typer.Exit(1)


def _looks_like_worklist(path: Path, scan_dir: Path) -> bool:
    if path.suffix.lower() != ".json":
        return False
    try:
        return path.resolve().parent == scan_dir.resolve()
    except OSError:
        return False


@app.command()
def ocr(
    path: Annotated[Path, typer.Argument(help="The file to OCR.")],
    langs: Annotated[Optional[str], typer.Option(help="Override TESSERACT_LANGS.")] = None,
) -> None:
    """Force OCR on a single file and print the recovered text."""
    log.info("CLI: ocr %s (langs=%s)", path, langs)
    from automafile.ocr import run_ocr
    text = run_ocr(path, langs=langs)
    typer.echo(text)


@app.command()
def scan(
    documents_root: Annotated[Optional[Path], typer.Option("--documents-root")] = None,
    path: Annotated[Optional[Path], typer.Option("--path", help="Limit the scan to a relative subpath under DOCUMENTS_ROOT (e.g. 'Inbox').")] = None,
    print_json: Annotated[bool, typer.Option("--json", help="Print the worklist instead of writing it.")] = False,
) -> None:
    """Run the scanner; emit a worklist JSON."""
    if documents_root is not None:
        import os
        os.environ["DOCUMENTS_ROOT"] = str(documents_root.resolve())
        from automafile.config import reset_settings
        reset_settings()
    log.info("CLI: scan (path=%s, json=%s)", path, print_json)
    from automafile.scanner import run_scan, write_worklist
    wl = run_scan(subpath=path)
    if print_json:
        typer.echo(json.dumps(wl.to_dict(), indent=2, ensure_ascii=False))
        return
    out = write_worklist(wl)
    if out is None:
        typer.echo(f"scan complete: seen={wl.files_seen}; everything is already in an existing worklist.")
        typer.echo("next: automafile process")
        return
    typer.echo(
        f"scan complete: seen={wl.files_seen} need_ocr={len(wl.files_needing_ocr)} "
        f"need_meta={len(wl.files_needing_metadata)} partial={len(wl.files_with_partial_metadata)} "
        f"stale={len(wl.files_with_stale_metadata)} ocr_review={len(wl.ocr_review_candidates)} "
        f"orphans={len(wl.orphan_sidecars)} quarantined={len(wl.quarantined_sidecars)} "
        f"unprocessable={len(wl.unprocessable_files)} (counts reflect new entries only)"
    )
    typer.echo(f"worklist: {out}")
    typer.echo(f"next: automafile process")


def _process_worklist(
    worklist: Optional[Path],
    settings,
    *,
    dry_run: bool,
    force_ocr: bool,
    stop_on_error: bool,
) -> None:
    from automafile.pipeline import format_result_line, process_file

    log.info("CLI: process worklist (worklist=%s dry_run=%s force_ocr=%s)", worklist, dry_run, force_ocr)
    expected_root = str(settings.documents_root)
    explicit = worklist is not None
    sources: list[Path]
    if explicit:
        sources = [worklist]
    else:
        # Sweep every candidate: remove unreadable ones and worklists whose
        # documents_root doesn't match (left over from a different host or
        # test run); merge what's left.
        sources = []
        for cand in sorted(settings.scan_dir.glob("scan-*.json")):
            try:
                cand_root = json.loads(cand.read_text(encoding="utf-8")).get("documents_root")
            except (OSError, json.JSONDecodeError) as exc:
                log.warning("process: removing unreadable worklist %s (%s)", cand.name, exc)
                cand.unlink(missing_ok=True)
                continue
            if cand_root == expected_root:
                sources.append(cand)
            else:
                log.info("process: removing stale worklist %s (root=%s)", cand.name, cand_root)
                cand.unlink(missing_ok=True)
        if not sources:
            typer.echo(f"no worklist found in {settings.scan_dir} for {expected_root}", err=True)
            raise typer.Exit(2)
        if len(sources) == 1:
            typer.echo(f"using worklist: {sources[0]}")
        else:
            typer.echo(f"merging {len(sources)} worklists for {expected_root}")

    buckets = (
        "files_needing_ocr",
        "files_needing_metadata",
        "files_with_partial_metadata",
        "files_with_stale_metadata",
    )
    seen: set[str] = set()
    todo: list[str] = []
    for src in sources:
        data = json.loads(src.read_text(encoding="utf-8"))
        wl_root = data.get("documents_root")
        if wl_root != expected_root:
            typer.echo(
                f"{src.name} was scanned for {wl_root}, but DOCUMENTS_ROOT is {expected_root}",
                err=True,
            )
            log.error("process: %s root mismatch (worklist=%s, current=%s)", src.name, wl_root, expected_root)
            raise typer.Exit(2)
        for bucket in buckets:
            for entry in data.get(bucket, []):
                rel = entry.get("relative_path")
                if rel and rel not in seen:
                    seen.add(rel)
                    todo.append(rel)

    if not todo:
        label = sources[0].name if len(sources) == 1 else f"{len(sources)} worklists"
        typer.echo(f"nothing to process in {label}")
        log.info("process: nothing to process across %d worklist(s)", len(sources))
        if not explicit:
            for src in sources:
                src.unlink(missing_ok=True)
        return

    label = sources[0].name if len(sources) == 1 else f"{len(sources)} worklists"
    typer.echo(f"processing {len(todo)} file(s) from {label}")
    log.info("process: %d file(s) from %d worklist(s)", len(todo), len(sources))
    failures = 0
    for rel in todo:
        path = settings.documents_root / rel
        if not path.exists():
            typer.echo(f"{rel} | MISSING under {settings.documents_root}")
            log.error("process: %s missing under %s", rel, settings.documents_root)
            failures += 1
            if stop_on_error:
                raise typer.Exit(1)
            continue
        result = process_file(path, dry_run=dry_run, force_ocr=force_ocr)
        typer.echo(format_result_line(result))
        if result.error:
            failures += 1
            if stop_on_error:
                raise typer.Exit(1)

    typer.echo(f"done: {len(todo) - failures}/{len(todo)} succeeded")
    log.info("process done: %d/%d succeeded", len(todo) - failures, len(todo))
    if not explicit and failures == 0:
        for src in sources:
            src.unlink(missing_ok=True)
        log.info("process: removed %d drained worklist(s)", len(sources))
    if failures:
        raise typer.Exit(1)


@app.command("review-ocr")
def review_ocr(
    yes_all: Annotated[bool, typer.Option("--yes-all", help="Re-OCR every candidate without asking.")] = False,
) -> None:
    """Walk OCR review candidates and act on each."""
    from automafile.config import get_settings
    from automafile.metadata.sidecar import read as sidecar_read, write as sidecar_write
    from automafile.metadata.schema import utc_now_iso
    from automafile.ocr import run_ocr, tesseract_version
    from automafile.scanner import run_scan

    log.info("CLI: review-ocr (yes_all=%s)", yes_all)
    settings = get_settings()
    wl = run_scan()
    if not wl.ocr_review_candidates:
        typer.echo("No OCR review candidates.")
        return

    for entry in wl.ocr_review_candidates:
        rel = entry["relative_path"]
        path = settings.documents_root / rel
        typer.echo(f"\nCandidate: {rel}")
        typer.echo(f"  previous: {entry.get('previous_engine')} / {entry.get('previous_languages')}")
        typer.echo(f"  current : {entry.get('current_engine')} / {entry.get('current_languages')}")
        choice = "y" if yes_all else typer.prompt("Re-OCR? [y/N/skip]", default="N")
        if choice.lower().startswith("y"):
            try:
                text = run_ocr(path)
                doc, summary, notes = sidecar_read(path)
                if doc is not None:
                    doc.ocr.engine = "tesseract"
                    doc.ocr.engine_version = tesseract_version()
                    doc.ocr.languages = settings.tesseract_langs
                    doc.ocr.done_at = utc_now_iso()
                    doc.metadata_modified = utc_now_iso()
                    sidecar_write(path, doc, summary or text[:1000], notes)
                typer.echo(f"  re-OCR'd ({len(text)} chars).")
            except Exception as exc:  # noqa: BLE001
                typer.echo(f"  failed: {exc}")
        elif choice.lower().startswith("s"):
            typer.echo("  skipped (will reappear on next scan)")
        else:
            doc, summary, notes = sidecar_read(path)
            if doc is not None:
                doc.metadata_modified = utc_now_iso()
                sidecar_write(path, doc, summary, notes)
            typer.echo("  declined; metadata_modified bumped.")


@app.command()
def reconcile(
    yes_all: Annotated[bool, typer.Option("--yes-all", help="Auto-accept hash-matched relinks when there is a single match.")] = False,
) -> None:
    """Walk orphan sidecars and propose hash-matched relinks."""
    from automafile.config import get_settings
    from automafile.metadata.reconcile import find_orphans
    from automafile.metadata.sidecar import update_relative_path, sidecar_path_for

    log.info("CLI: reconcile (yes_all=%s)", yes_all)
    settings = get_settings()
    orphans = find_orphans(settings.documents_root)
    if not orphans:
        typer.echo("No orphan sidecars.")
        return

    for orphan in orphans:
        typer.echo(f"\nOrphan: {orphan.sidecar_path}")
        typer.echo(f"  described file: {orphan.described_relative_path}")
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
        # the orphan's described filename + sidecar location implies the original path
        # described as <sidecar.parent.parent>/<filename>; fabricate a stand-in path
        original = orphan.sidecar_path.parent.parent / orphan.described_filename
        update_relative_path(original, target)
        typer.echo(f"  relinked to {target}")


@app.command()
def bootstrap(
    force: Annotated[bool, typer.Option("--force", help="Overwrite memory templates even if present.")] = False,
) -> None:
    """Seed memory templates and create folder layout. Idempotent."""
    log.info("CLI: bootstrap (force=%s)", force)
    from automafile.bootstrap import bootstrap as bootstrap_fn
    bootstrap_fn(force=force)


@app.command()
def doctor() -> None:
    """Diagnose the local environment (Tesseract, Ollama, paths)."""
    log.info("CLI: doctor")
    from automafile.config import get_settings
    from automafile.llm import ollama_available, ollama_has_model
    from automafile.ocr import tesseract_available, tesseract_languages, tesseract_version

    settings = get_settings()
    typer.echo(f"Documents root: {settings.documents_root}{'  (exists)' if settings.documents_root.exists() else '  (missing)'}")
    typer.echo(f"  inbox: {settings.inbox_path}{'  (exists)' if settings.inbox_path.exists() else '  (missing)'}")

    tess = tesseract_available()
    typer.echo(f"Tesseract present: {tess}")
    if tess:
        typer.echo(f"  version  : {tesseract_version()}")
        typer.echo(f"  langs    : {', '.join(tesseract_languages()) or '(unknown)'}")

    available = ollama_available()
    typer.echo(f"Ollama reachable : {available}  ({settings.ollama_url})")
    if available:
        typer.echo(f"  model present : {ollama_has_model()}  ({settings.ollama_model})")


@app.command("filer-apply")
def filer_apply_cmd(
    path: Annotated[Path, typer.Argument(help="The file to file.")],
    category: Annotated[str, typer.Option(help="Top-level category.")] = "Unknown",
    subcategory: Annotated[Optional[str], typer.Option(help="Optional subcategory.")] = None,
    name: Annotated[Optional[str], typer.Option(help="Smart filename. Auto-derived if omitted.")] = None,
    overwrite: Annotated[bool, typer.Option("--overwrite", help="Overwrite the target if it exists with different content.")] = False,
) -> None:
    r"""Move ``path`` into the managed tree under ``category\[/subcategory]/<name>``."""
    log.info("CLI: filer-apply %s (category=%s subcategory=%s overwrite=%s)", path, category, subcategory, overwrite)
    from automafile.filer import FilingProposal, apply_filing, smart_filename
    from automafile.metadata.sidecar import read as sidecar_read

    if name is None:
        doc, summary, _ = sidecar_read(path)
        meta = {}
        if doc is not None:
            meta = {
                "title": doc.title,
                "summary": summary or "",
                "correspondent": doc.correspondent,
                "date": doc.date,
                "category": category,
                "subcategory": subcategory,
            }
        meta["extension"] = path.suffix.lstrip(".")
        name = smart_filename(meta, path.suffix.lstrip("."))

    proposal = FilingProposal(category=category, subcategory=subcategory, smart_name=name)
    target = apply_filing(path, proposal, overwrite=overwrite)
    typer.echo(f"filed: {target}")


def main() -> None:  # pragma: no cover
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
