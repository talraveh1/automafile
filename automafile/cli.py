"""Typer-based CLI for Automafile."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated, Optional

import typer

from automafile import __version__


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
    from automafile.watcher import run_watcher
    run_watcher()


@app.command()
def process(
    path: Annotated[Path, typer.Argument(help="The file to process.")],
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Run extraction + LLM but write nothing.")] = False,
    force_ocr: Annotated[bool, typer.Option("--force-ocr", help="Force OCR even if not recommended.")] = False,
) -> None:
    """Process a single file end-to-end."""
    from automafile.pipeline import format_result_line, process_file
    result = process_file(path, dry_run=dry_run, force_ocr=force_ocr)
    typer.echo(format_result_line(result))
    if result.error:
        raise typer.Exit(1)


@app.command()
def ocr(
    path: Annotated[Path, typer.Argument(help="The file to OCR.")],
    langs: Annotated[Optional[str], typer.Option(help="Override TESSERACT_LANGS.")] = None,
) -> None:
    """Force OCR on a single file and print the recovered text."""
    from automafile.ocr import run_ocr
    text = run_ocr(path, langs=langs)
    typer.echo(text)


@app.command()
def scan(
    documents_root: Annotated[Optional[Path], typer.Option("--documents-root")] = None,
    print_json: Annotated[bool, typer.Option("--json", help="Print the worklist instead of writing it.")] = False,
) -> None:
    """Run the scanner; emit a worklist JSON."""
    if documents_root is not None:
        import os
        os.environ["DOCUMENTS_ROOT"] = str(documents_root.resolve())
        from automafile.config import reset_settings
        reset_settings()
    from automafile.scanner import run_scan, write_worklist
    wl = run_scan()
    if print_json:
        typer.echo(json.dumps(wl.to_dict(), indent=2, ensure_ascii=False))
        return
    out = write_worklist(wl)
    typer.echo(
        f"scan complete: seen={wl.files_seen} need_ocr={len(wl.files_needing_ocr)} "
        f"need_meta={len(wl.files_needing_metadata)} partial={len(wl.files_with_partial_metadata)} "
        f"stale={len(wl.files_with_stale_metadata)} ocr_review={len(wl.ocr_review_candidates)} "
        f"orphans={len(wl.orphan_sidecars)} quarantined={len(wl.quarantined_sidecars)} "
        f"unprocessable={len(wl.unprocessable_files)}"
    )
    typer.echo(f"worklist: {out}")


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
    from automafile.bootstrap import bootstrap as bootstrap_fn
    bootstrap_fn(force=force)


@app.command()
def doctor() -> None:
    """Diagnose the local environment (Tesseract, Ollama, paths)."""
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
    """Move ``path`` into the managed tree under ``category[/subcategory]/<name>``."""
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
