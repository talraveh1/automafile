"""`dnd toaster` — control the Windows toaster (host-only)."""

from __future__ import annotations

import sys
from typing import Annotated

import typer

from dragndoc.cli import toaster_app
from dragndoc.cli._common import _print_status
from dragndoc.log import get_logger


log = get_logger(__name__)


@toaster_app.command("start")
def toaster_start(
    fg: Annotated[bool, typer.Option("--fg", help="Run the toaster in this process instead of spawning a detached one.")] = False,
    no_tray: Annotated[bool, typer.Option("--no-tray", help="Run headless (no tray icon). For debugging or pipes.")] = False,
) -> None:
    """Start the toaster (background by default; ``--fg`` to run in this terminal)."""
    log.info("CLI: toaster start (fg=%s no_tray=%s)", fg, no_tray)
    from dragndoc.toaster import start_background, start_foreground

    if fg:
        raise typer.Exit(start_foreground(tray=not no_tray))
    raise typer.Exit(start_background(tray=not no_tray))


@toaster_app.command("stop")
def toaster_stop(
    timeout: Annotated[float, typer.Option("--timeout", min=0.1, help="Max seconds to wait for the toaster to exit.")] = 10.0,
) -> None:
    """Stop the running toaster."""
    log.info("CLI: toaster stop (timeout=%s)", timeout)
    from dragndoc.toaster import stop_toaster
    raise typer.Exit(stop_toaster(timeout=timeout))


@toaster_app.command("restart")
def toaster_restart(
    timeout: Annotated[float, typer.Option("--timeout", min=0.1, help="Max seconds to wait for the toaster to exit before relaunching.")] = 10.0,
    no_tray: Annotated[bool, typer.Option("--no-tray", help="Run headless (no tray icon). For debugging or pipes.")] = False,
) -> None:
    """Restart the running background toaster."""
    log.info("CLI: toaster restart (timeout=%s no_tray=%s)", timeout, no_tray)
    from dragndoc.toaster import restart_toaster
    raise typer.Exit(restart_toaster(tray=not no_tray, timeout=timeout))


@toaster_app.command("status")
def toaster_status() -> None:
    """Show whether the toaster is running, plus install state (shortcut + AUMID)."""
    log.info("CLI: toaster status")
    from dragndoc.toaster import status_snapshot

    _print_status("toaster", status_snapshot())

    if sys.platform == "win32":
        from dragndoc.toaster_setup import status as setup_status
        setup_status()


@toaster_app.command("install")
def toaster_install() -> None:
    """Install the Windows Startup shortcut + register the AUMID."""
    if sys.platform != "win32":
        typer.echo("install is Windows-only", err=True)
        raise typer.Exit(2)
    log.info("CLI: toaster install")
    from dragndoc.toaster_setup import install
    raise typer.Exit(install())


@toaster_app.command("uninstall")
def toaster_uninstall() -> None:
    """Remove the Windows Startup shortcut + unregister the AUMID."""
    if sys.platform != "win32":
        typer.echo("uninstall is Windows-only", err=True)
        raise typer.Exit(2)
    log.info("CLI: toaster uninstall")
    from dragndoc.toaster_setup import uninstall
    raise typer.Exit(uninstall())
