"""`dnd bootstrap` and `dnd doctor` — top-level utilities."""

from __future__ import annotations

from typing import Annotated

import typer

from dragndoc.cli import app
from dragndoc.log import get_logger


log = get_logger(__name__)


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
    """Diagnose the local environment (Tesseract, Whisper, Ollama, paths)."""
    log.info("CLI: doctor")
    from dragndoc.config import get_settings
    from dragndoc.llm import ollama_available, ollama_has_model
    from dragndoc.mux import mkvmerge_available, mkvmerge_version
    from dragndoc.ocr import tesseract_available, tesseract_languages, tesseract_version
    from dragndoc.transcribe import (
        ffmpeg_available,
        ffprobe_available,
        whisper_available,
        whisper_version,
    )

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

    typer.echo(f"ffmpeg present : {ffmpeg_available()}")
    typer.echo(f"ffprobe present: {ffprobe_available()}")
    mkv = mkvmerge_available()
    typer.echo(f"mkvmerge present: {mkv}")
    if mkv:
        typer.echo(f"  version: {mkvmerge_version()}")
    whisper = whisper_available()
    typer.echo(f"Whisper present: {whisper}")
    if whisper:
        typer.echo(f"  model    : {settings.asr.model}")
        typer.echo(f"  version  : {whisper_version()}")
        typer.echo(f"  device   : {settings.asr.device}")
        typer.echo(f"  langs    : {settings.asr.langs}")

    available = ollama_available()
    typer.echo(f"Ollama reachable : {available}  ({settings.ollama.url})")
    if available:
        typer.echo(f"  model present : {ollama_has_model()}  ({settings.ollama.model})")
