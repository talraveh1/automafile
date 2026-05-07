"""`dnd triage` — inspect / drain the triage queue."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Annotated

import typer

from dragndoc.cli import triage_app


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
