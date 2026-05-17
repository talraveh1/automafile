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


@review_app.command("proposals")
def review_proposals(
    kind: Annotated[str | None, typer.Option("--kind", help="Filter by proposal kind (recording_type / speaker_name / dir_mode).")] = None,
    subject: Annotated[str | None, typer.Option("--subject", help="Filter by subject (e.g. 'doc:47' or 'dir:Inbox/voix').")] = None,
    yes_all: Annotated[bool, typer.Option("--yes-all", help="Accept every pending proposal without asking.")] = False,
    list_only: Annotated[bool, typer.Option("--list", help="List pending proposals; don't prompt.")] = False,
) -> None:
    """Walk pending proposals (recording_type, speaker_name, dir_mode) for accept/edit/reject."""
    from dragndoc import proposals as proposals_mod

    log.info("CLI: review proposals (kind=%s, subject=%s, yes_all=%s, list=%s)",
             kind, subject, yes_all, list_only)
    pending = proposals_mod.list_pending(kind=kind, subject=subject)
    if not pending:
        typer.echo("No pending proposals.")
        return

    for proposal in pending:
        typer.echo("")
        typer.echo(f"#{proposal.id} [{proposal.kind}] subject={proposal.subject}")
        typer.echo(f"  source: {proposal.source}")
        if proposal.rationale:
            typer.echo(f"  why   : {proposal.rationale}")
        typer.echo(f"  value : {proposal.value}")

        if list_only:
            continue

        if yes_all:
            proposals_mod.accept(proposal.id)
            _apply_accepted_proposal(proposal)
            typer.echo("  → accepted")
            continue

        choice = typer.prompt("  [a]ccept / [r]eject / [s]kip", default="s")
        c = (choice or "").lower()[:1]
        if c == "a":
            proposals_mod.accept(proposal.id)
            _apply_accepted_proposal(proposal)
            typer.echo("  → accepted")
        elif c == "r":
            proposals_mod.reject(proposal.id)
            typer.echo("  → rejected")
        else:
            typer.echo("  → skipped (still pending)")


def _apply_accepted_proposal(proposal) -> None:
    """Mirror an accepted proposal into the relevant committed-truth row.

    - recording_type → asr.recording_type for the doc
    - speaker_name → asr.speakers semilist (additive) + regenerate SRT
    - dir_mode → dirs.mode + dirs.source='proposal'
    """
    from dragndoc.proposals import (
        KIND_DIR_MODE, KIND_RECORDING_TYPE, KIND_SPEAKER_NAME,
    )
    if proposal.kind == KIND_RECORDING_TYPE:
        _commit_recording_type(proposal)
    elif proposal.kind == KIND_SPEAKER_NAME:
        _commit_speaker_name(proposal)
    elif proposal.kind == KIND_DIR_MODE:
        _commit_dir_mode(proposal)


def _commit_recording_type(proposal) -> None:
    from dragndoc.db import transaction
    rec_type = proposal.value.get("recording_type") or "unknown"
    doc_id = _doc_id_from_subject(proposal.subject)
    if doc_id is None:
        return
    with transaction() as conn:
        conn.execute(
            "UPDATE asr SET recording_type = ? WHERE doc_id = ?",
            (rec_type, doc_id),
        )


def _commit_speaker_name(proposal) -> None:
    """Apply a single SPEAKER_XX -> name mapping. Regenerates SRT sidecar."""
    from pathlib import Path
    from dragndoc import asr_artifacts
    from dragndoc.db import connect, transaction
    from dragndoc.config import get_settings
    from dragndoc.meta_store import from_semilist, to_semilist

    doc_id = _doc_id_from_subject(proposal.subject)
    if doc_id is None:
        return
    label = proposal.value.get("label") or ""
    name = proposal.value.get("name") or ""
    if not label or not name:
        return

    # update speakers semilist + remap any SRT segment speakers
    with connect(readonly=True) as conn:
        row = conn.execute(
            "SELECT d.path, a.speakers FROM docs d "
            "LEFT JOIN asr a ON a.doc_id = d.id WHERE d.id = ?",
            (doc_id,),
        ).fetchone()
    if not row:
        return
    speakers = from_semilist(row["speakers"] or "")
    if name not in speakers:
        speakers.append(name)

    # rewrite the JSON twin's segment speakers + regenerate SRT
    payload = asr_artifacts.load_json(doc_id)
    if payload is not None:
        for seg in payload.segments:
            if seg.speaker == label:
                seg.speaker = name
        payload.srt = ""  # force regenerate from segments below
        # write JSON back + new SRT
        from dragndoc.transcribe import to_srt
        payload.srt = to_srt(payload.segments)
        settings = get_settings()
        docs_root = settings.docs
        original = docs_root / row["path"]
        asr_artifacts.save(payload, original=original, doc_id=doc_id, force=True)

    with transaction() as conn:
        conn.execute(
            "UPDATE asr SET speakers = ? WHERE doc_id = ?",
            (to_semilist(speakers), doc_id),
        )


def _commit_dir_mode(proposal) -> None:
    from dragndoc.db import transaction
    from dragndoc.meta_store import utc_now_iso

    rel_path = proposal.subject.removeprefix("dir:")
    mode = proposal.value.get("mode") or "unknown"
    with transaction() as conn:
        conn.execute(
            "INSERT INTO dirs (path, mode, source, decided_at) VALUES (?, ?, 'proposal', ?) "
            "ON CONFLICT(path) DO UPDATE SET mode = excluded.mode, source = 'proposal', decided_at = excluded.decided_at",
            (rel_path, mode, utc_now_iso()),
        )


def _doc_id_from_subject(subject: str) -> int | None:
    if not subject.startswith("doc:"):
        return None
    try:
        return int(subject.removeprefix("doc:"))
    except ValueError:
        return None
