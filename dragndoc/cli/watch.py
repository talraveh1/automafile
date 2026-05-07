"""`dnd watch` — control the file watcher."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer

from dragndoc.cli import watch_app
from dragndoc.cli._common import _maybe_override_docs, _print_status
from dragndoc.log import get_logger


log = get_logger(__name__)


def _run_watch_foreground(docs: Path | None) -> None:
    _maybe_override_docs(docs)
    log.info("CLI: watch (docs=%s)", docs)
    from dragndoc.watcher import run_watcher

    run_watcher()


def _request_watch_stop(*, wait: bool, timeout: float) -> None:
    log.info("CLI: watch stop (wait=%s timeout=%s)", wait, timeout)
    from dragndoc.runtime import request_stop, wait_for_running

    request_stop()
    if wait and not wait_for_running(False, timeout=timeout):
        typer.echo("watcher stop request sent, but it did not stop before the timeout", err=True)
        raise typer.Exit(1)
    typer.echo("watcher stop requested")


def _request_watch_start(*, fg: bool, docs: Path | None, wait: bool, timeout: float) -> None:
    from dragndoc.runtime import request_start, status_snapshot, wait_for_running

    if fg:
        snapshot = status_snapshot()
        if bool(snapshot["running"]):
            typer.echo("supervised watcher is already running; stop it first or use the existing background watcher", err=True)
            raise typer.Exit(1)
        log.info("CLI: watch start --fg (docs=%s)", docs)
        _run_watch_foreground(docs)
        return

    if docs is not None:
        typer.echo("--docs is only supported with --fg", err=True)
        raise typer.Exit(2)

    snapshot = status_snapshot()
    if bool(snapshot["running"]):
        pid = snapshot["pid"]
        typer.echo(f"watcher already running (pid={pid})")
        return

    log.info("CLI: watch start (wait=%s timeout=%s)", wait, timeout)
    request_start()
    if wait and not wait_for_running(True, timeout=timeout):
        typer.echo(
            "background watcher start was requested, but no supervisor started it before the timeout; "
            "use `dnd watch supervise` or `dnd watch start --fg`",
            err=True,
        )
        raise typer.Exit(1)
    typer.echo("watcher start requested")


def _show_watch_status() -> None:
    log.info("CLI: watch status")
    from dragndoc.runtime import status_snapshot

    _print_status("watcher", status_snapshot())


@watch_app.callback()
def watch(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is not None:
        return
    typer.echo(ctx.get_help(), nl=False)
    raise typer.Exit(0)


@watch_app.command("supervise")
def watch_supervise() -> None:
    """Run the container supervisor that owns the watcher process."""
    log.info("CLI: watch supervise")
    from dragndoc.runtime import supervise as supervise_runtime

    raise typer.Exit(supervise_runtime())


@watch_app.command("start")
def watch_start(
    fg: Annotated[bool, typer.Option("--fg", help="Run the watcher in the foreground instead of resuming the supervised background watcher.")] = False,
    docs: Annotated[Optional[Path], typer.Option("--docs", help="Override DOCS when using --fg.")] = None,
    wait: Annotated[bool, typer.Option("--wait/--no-wait", help="Wait until the background watcher is running.")] = True,
    timeout: Annotated[float, typer.Option("--timeout", min=0.1, help="Max seconds to wait when --wait is set.")] = 10.0,
) -> None:
    """Start or resume the watcher."""
    _request_watch_start(fg=fg, docs=docs, wait=wait, timeout=timeout)


@watch_app.command("stop")
def watch_stop(
    wait: Annotated[bool, typer.Option("--wait/--no-wait", help="Wait until the watcher has stopped.")] = True,
    timeout: Annotated[float, typer.Option("--timeout", min=0.1, help="Max seconds to wait when --wait is set.")] = 10.0,
) -> None:
    """Stop the supervised watcher without exiting the container."""
    _request_watch_stop(wait=wait, timeout=timeout)


@watch_app.command("status")
def watch_status() -> None:
    """Show whether the supervised watcher is running, stopped, or idle."""
    _show_watch_status()
