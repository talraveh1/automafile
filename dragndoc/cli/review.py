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
