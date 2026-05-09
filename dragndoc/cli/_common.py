"""Shared helpers used by more than one CLI sub-app module."""

from __future__ import annotations

import os
from pathlib import Path

import typer


def _maybe_override_docs(docs: Path | None) -> None:
    if docs is None:
        return
    os.environ["DOCS"] = str(docs.resolve())
    # reset cached settings so later config reads see the docs override immediately
    from dragndoc.config import reset_settings

    reset_settings()


def _print_status(label: str, snapshot: dict) -> None:
    """Print a one-line process status using the project's standard format."""
    state = snapshot["state"]
    pid = snapshot["pid"]
    if pid is None:
        typer.echo(f"{label}: {state}")
    else:
        typer.echo(f"{label}: {state} (pid={pid})")
