"""`dnd review` — walk metadata that needs human attention."""

from __future__ import annotations

from typing import Annotated

import typer

from dragndoc.cli import review_app
from dragndoc.log import get_logger


log = get_logger(__name__)


@review_app.command("ocr")
def review_ocr(
    yes_all: Annotated[bool, typer.Option("--yes-all", help="Re-OCR every candidate without asking.")] = False,
) -> None:
    """Walk OCR review candidates (engine/lang drift since extraction)."""
    from dragndoc.config import get_settings
    from dragndoc.meta_store import OcrInfo, get_by_path, upsert
    from dragndoc.ocr import run_ocr
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
                    doc.ocr = OcrInfo.for_tesseract_run("ocr_full")
                    upsert(doc)
                typer.echo("  re-OCR'd.")
            except Exception as exc:  # noqa: BLE001
                typer.echo(f"  Failed: {exc}")
        elif choice.lower().startswith("s"):
            typer.echo("  Skipped (will reappear on next scan)")
        else:
            typer.echo("  Declined.")


@review_app.command("orphans")
def review_orphans(
    apply: Annotated[bool, typer.Option("--apply", help="Apply proposed relinks instead of previewing them.")] = False,
) -> None:
    """Preview rows whose file is missing on disk and hash-matched relinks."""
    from dragndoc.scanner import run_scan

    log.info("CLI: review orphans (apply=%s)", apply)
    report = run_scan(apply=apply)
    recon = report.reconciliation
    if not recon.renames and not recon.merges and not recon.unresolved_orphans:
        typer.echo("No orphan rows.")
        return
    action = "Applied" if apply else "Proposed"
    for old, new in recon.renames:
        typer.echo(f"{action} rename: {old} -> {new}")
    for merge in recon.merges:
        typer.echo(f"{action} merge: {merge.old_path} -> {merge.new_path} winner={merge.winner_id} loser={merge.loser_id}")
    for orphan in recon.unresolved_orphans:
        typer.echo(f"Unresolved: {orphan.recorded_path} id={orphan.doc_id} reason={orphan.reason}")
