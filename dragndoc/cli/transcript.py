"""`dnd transcript` — inspect and edit ASR-produced transcripts.

Triage and other LLM-assisted flows use these to fetch the full
spoken-content view (SRT + speaker labels + timing) for an audio /
video document — beyond what the bounded LLM summary in
``docs.summary`` captures.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from dragndoc.cli import transcript_app
from dragndoc.log import get_logger


log = get_logger(__name__)


def _require_audio_doc(path: Path):
    """Look up ``path`` in docs; abort if no row or no SRT sidecar tracked."""
    from dragndoc.meta_store import get_by_file

    doc = get_by_file(path)
    if doc is None:
        typer.echo(f"No row for: {path}", err=True)
        raise typer.Exit(1)
    return doc


def _resolve_srt_path(doc, fallback: Path) -> Path:
    """Pick the SRT for ``doc``: prefer the DB-tracked path, fall back to <base>.srt."""
    from dragndoc import asr_artifacts
    from dragndoc.config import get_settings

    srt_rel = doc.asr.srt_path if doc.asr else None
    if srt_rel:
        return get_settings().docs / srt_rel
    return asr_artifacts.srt_sidecar_for(fallback)


@transcript_app.command("show")
def transcript_show(
    paths: Annotated[list[Path], typer.Argument(help="One or more audio/video files, directories, or glob patterns.")],
    json_view: Annotated[bool, typer.Option("--json", help="Print the JSON twin instead of the SRT.")] = False,
    recursive: Annotated[bool, typer.Option("-r", "--recursive", help="When an argument is a directory, walk its whole subtree.")] = False,
    insensitive: Annotated[bool, typer.Option("-i", "--insensitive", help="Case-insensitive glob matching.")] = False,
) -> None:
    """Print the full SRT sidecar (or JSON twin) for one or more audio/video files.

    The SRT lives next to the original as ``<basename>.srt``; the JSON twin
    lives under ``data/transcripts/<doc-id>.json``.
    """
    from dragndoc import asr_artifacts
    from dragndoc.cli._path_args import expand_paths
    from dragndoc.meta_store import get_by_file

    log.info("CLI: transcript show %d arg(s) (json=%s recursive=%s insensitive=%s)",
             len(paths), json_view, recursive, insensitive)
    expanded = expand_paths(paths, recursive=recursive, insensitive=insensitive)
    if not expanded:
        typer.echo(f"No matching files: {', '.join(str(p) for p in paths)}", err=True)
        raise typer.Exit(1)

    failures = 0
    for i, fp in enumerate(expanded):
        doc = get_by_file(fp)
        if doc is None:
            if len(expanded) == 1:
                typer.echo(f"No row for: {fp}", err=True)
                raise typer.Exit(1)
            failures += 1
            continue

        if json_view:
            jp = asr_artifacts.json_path_for(doc.id) if doc.id else None
            if not jp or not jp.exists():
                if len(expanded) == 1:
                    typer.echo(f"No JSON twin for {fp}", err=True)
                    raise typer.Exit(1)
                failures += 1
                continue
            if len(expanded) > 1:
                if i:
                    typer.echo("\n---\n")
                typer.echo(f"## {fp}\n")
            typer.echo(jp.read_text(encoding="utf-8"))
            continue

        srt = _resolve_srt_path(doc, fp)
        if not srt.exists():
            if len(expanded) == 1:
                typer.echo(f"No SRT sidecar for {fp} (looked at {srt})", err=True)
                raise typer.Exit(1)
            failures += 1
            continue
        if len(expanded) > 1:
            if i:
                typer.echo("\n---\n")
            typer.echo(f"## {fp}\n")
        typer.echo(srt.read_text(encoding="utf-8"))

    if failures and len(expanded) > 1:
        typer.echo(f"\n[skipped {failures} files with no transcript]", err=True)


@transcript_app.command("path")
def transcript_path(
    paths: Annotated[list[Path], typer.Argument(help="One or more audio/video files, directories, or glob patterns.")],
    json_view: Annotated[bool, typer.Option("--json", help="Print the JSON twin path instead of the SRT.")] = False,
    recursive: Annotated[bool, typer.Option("-r", "--recursive", help="When an argument is a directory, walk its whole subtree.")] = False,
    insensitive: Annotated[bool, typer.Option("-i", "--insensitive", help="Case-insensitive glob matching.")] = False,
) -> None:
    """Print resolved SRT (or JSON) paths — one per line. Useful for tooling."""
    from dragndoc import asr_artifacts
    from dragndoc.cli._path_args import expand_paths
    from dragndoc.meta_store import get_by_file

    log.info("CLI: transcript path %d arg(s) (json=%s)", len(paths), json_view)
    expanded = expand_paths(paths, recursive=recursive, insensitive=insensitive)
    if not expanded:
        raise typer.Exit(1)

    for fp in expanded:
        doc = get_by_file(fp)
        if doc is None:
            continue
        if json_view:
            jp = asr_artifacts.json_path_for(doc.id) if doc.id else None
            if jp:
                typer.echo(str(jp))
        else:
            srt = _resolve_srt_path(doc, fp)
            typer.echo(str(srt))
