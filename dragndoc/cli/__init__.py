"""Typer-based CLI for Drag'n'Doc."""

from __future__ import annotations

from typing import Annotated

import typer

from dragndoc import __version__


HELP_CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"]}


app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    context_settings=HELP_CONTEXT_SETTINGS,
    help="Drag'n'Doc — watch a folder, enrich files with metadata, file them via Claude.",
)
watch_app = typer.Typer(
    add_completion=False,
    invoke_without_command=True,
    context_settings=HELP_CONTEXT_SETTINGS,
    help="Control the watcher.",
)
review_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    context_settings=HELP_CONTEXT_SETTINGS,
    help="Walk metadata that needs human attention.",
)
meta_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    context_settings=HELP_CONTEXT_SETTINGS,
    help="Inspect and edit document metadata rows.",
)
toaster_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    context_settings=HELP_CONTEXT_SETTINGS,
    help="Control the Windows toaster (run on the host).",
)
triage_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    context_settings=HELP_CONTEXT_SETTINGS,
    help="Inspect / drain the triage queue (filled by digest, drained by /triage).",
)
app.add_typer(watch_app, name="watch")
app.add_typer(review_app, name="review")
app.add_typer(meta_app, name="meta")
app.add_typer(toaster_app, name="toaster")
app.add_typer(triage_app, name="triage")


@app.callback()
def _root(version: Annotated[bool, typer.Option("--version", help="Print version and exit.")] = False) -> None:
    if version:
        typer.echo(f"dragndoc {__version__}")
        raise typer.Exit(0)


def main() -> None:  # pragma: no cover
    app()


# Side-effect imports register subcommand decorators on the apps defined above.
from dragndoc.cli import (  # noqa: E402, F401
    digest,
    files,
    grep,
    meta,
    misc,
    review,
    toaster,
    triage,
    watch,
)


if __name__ == "__main__":  # pragma: no cover
    main()
