"""`dnd dir` — inspect and override directory-mode metadata."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Optional

import typer

from dragndoc.cli import dir_app
from dragndoc.dirs import DIR_MODES, ensure_tracked, get_dir, list_dirs, set_mode


@dir_app.command("get")
def dir_get(
    path: Annotated[Path, typer.Argument(help="Directory path.")],
) -> None:
    """Print the tracked directory row as JSON."""
    row = get_dir(path)
    if row is None and path.exists() and path.is_dir():
        row = ensure_tracked(path)
    if row is None:
        typer.echo(f"No directory row for: {path}", err=True)
        raise typer.Exit(1)
    typer.echo(json.dumps(row.to_dict(), indent=2, ensure_ascii=False, default=str))


@dir_app.command("set")
def dir_set(
    path: Annotated[Path, typer.Argument(help="Directory path.")],
    mode: Annotated[str, typer.Option("--mode", help="Directory mode: collection, bundle, or opaque.")],
) -> None:
    """Set a directory mode manually."""
    try:
        row = set_mode(path, mode)
    except ValueError as exc:
        valid = ", ".join(sorted(DIR_MODES - {"unknown"}))
        typer.echo(f"{exc}; expected one of: {valid}", err=True)
        raise typer.Exit(2) from None
    typer.echo(f"Updated: {row.path} ({row.mode})")


@dir_app.command("ls")
def dir_ls(
    parent: Annotated[Optional[Path], typer.Argument(help="Optional parent path.")] = None,
) -> None:
    """List tracked directories, optionally under a parent path."""
    rows = list_dirs(parent)
    for row in rows:
        typer.echo(json.dumps(row.to_dict(), ensure_ascii=False, default=str))


@dir_app.command("classify")
def dir_classify(
    path: Annotated[Path, typer.Argument(help="Directory to classify (calls Ollama).")],
) -> None:
    """Classify a directory via LLM and enqueue a proposal for `dnd review proposals`."""
    from dragndoc import dir_classifier

    if not path.exists() or not path.is_dir():
        typer.echo(f"Not a directory: {path}", err=True)
        raise typer.Exit(1)
    proposal_id = dir_classifier.enqueue_for(path)
    if proposal_id:
        typer.echo(f"Enqueued dir_mode proposal #{proposal_id} for {path}")
    else:
        typer.echo(f"Failed to enqueue proposal for {path}", err=True)
        raise typer.Exit(1)
