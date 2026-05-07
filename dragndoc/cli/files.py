"""`dnd mv`, `dnd cp`, `dnd rm`, `dnd ls` — file operations that keep DB rows in sync."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from dragndoc.cli import app
from dragndoc.log import get_logger


log = get_logger(__name__)


def _resolve_move_target(src: Path, dst: Path, *, force: bool) -> Path:
    """Validate ``src`` is a file and resolve the destination for ``mv``/``cp``."""
    if not src.exists():
        typer.echo(f"src not found: {src}", err=True)
        raise typer.Exit(1)
    if not src.is_file():
        typer.echo(f"src is not a file: {src}", err=True)
        raise typer.Exit(1)

    target = dst / src.name if dst.exists() and dst.is_dir() else dst
    if target.resolve() == src.resolve():
        typer.echo(f"src and dst are the same: {src}", err=True)
        raise typer.Exit(1)
    if target.exists() and not force:
        typer.echo(f"target exists: {target} (use -f to overwrite)", err=True)
        raise typer.Exit(1)

    target.parent.mkdir(parents=True, exist_ok=True)
    return target


@app.command()
def mv(
    src: Annotated[Path, typer.Argument(help="Source file path.")],
    dst: Annotated[Path, typer.Argument(help="Destination file path or directory.")],
    force: Annotated[bool, typer.Option("-f", "--force", help="Overwrite the target file if it exists.")] = False,
) -> None:
    """Move a file. Updates the metadata row's path."""
    import shutil as _shutil
    from dragndoc.db import transaction
    from dragndoc.meta_store import relative_to_root

    target = _resolve_move_target(src, dst, force=force)
    log.info("CLI: mv %s -> %s (force=%s)", src, target, force)
    src_rel = relative_to_root(src)
    _shutil.move(str(src), str(target))
    new_rel = relative_to_root(target)
    with transaction() as conn:
        conn.execute("UPDATE docs SET path = ? WHERE path = ?", (new_rel, src_rel))
    typer.echo(f"moved: {target}")


@app.command()
def cp(
    src: Annotated[Path, typer.Argument(help="Source file path.")],
    dst: Annotated[Path, typer.Argument(help="Destination file path or directory.")],
    force: Annotated[bool, typer.Option("-f", "--force", help="Overwrite the target file if it exists.")] = False,
) -> None:
    """Copy a file. Duplicates the metadata row at the new path (same hash)."""
    import shutil as _shutil
    from dragndoc.meta_store import get_by_file, relative_to_root, upsert

    target = _resolve_move_target(src, dst, force=force)
    log.info("CLI: cp %s -> %s (force=%s)", src, target, force)
    _shutil.copy2(str(src), str(target))
    src_doc = get_by_file(src)
    if src_doc is not None:
        src_doc.id = None  # force INSERT path on upsert
        src_doc.path = relative_to_root(target)
        upsert(src_doc)
    typer.echo(f"copied: {target}")


@app.command()
def rm(
    path: Annotated[Path, typer.Argument(help="File path to remove.")],
    force: Annotated[bool, typer.Option("-f", "--force", help="Ignore missing file and exit successfully.")] = False,
) -> None:
    """Remove a file and its metadata row."""
    from dragndoc.meta_store import delete_by_path, relative_to_root

    if not path.exists():
        if force:
            return
        typer.echo(f"not found: {path}", err=True)
        raise typer.Exit(1)
    if not path.is_file():
        typer.echo(f"not a file: {path}", err=True)
        raise typer.Exit(1)

    log.info("CLI: rm %s (force=%s)", path, force)
    rel = relative_to_root(path)
    path.unlink()
    delete_by_path(rel)
    typer.echo(f"removed: {path}")


@app.command()
def ls(
    path: Annotated[Path, typer.Argument(help="Directory to list.")] = Path("."),
    show_all: Annotated[bool, typer.Option("-a", "--all", help="Show entries that start with a dot.")] = False,
) -> None:
    """List a directory; files that have a metadata row are marked with ``*``."""
    from dragndoc.db import connect
    from dragndoc.meta_store import relative_to_root

    if not path.exists():
        typer.echo(f"not found: {path}", err=True)
        raise typer.Exit(1)
    if not path.is_dir():
        typer.echo(f"not a directory: {path}", err=True)
        raise typer.Exit(1)

    entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    files_for_query = [e for e in entries if e.is_file() and (show_all or not e.name.startswith("."))]
    rels = [relative_to_root(f) for f in files_for_query]
    rows: set[str] = set()
    if rels:
        placeholders = ",".join("?" * len(rels))
        with connect(readonly=True) as conn:
            for r in conn.execute(
                f"SELECT path FROM docs WHERE path IN ({placeholders})", rels
            ).fetchall():
                rows.add(r["path"])

    rel_iter = iter(rels)
    for entry in entries:
        if not show_all and entry.name.startswith("."):
            continue
        if entry.is_dir():
            typer.echo(f"  {entry.name}/")
        else:
            rel = next(rel_iter)
            mark = "*" if rel in rows else " "
            typer.echo(f"{mark} {entry.name}")
