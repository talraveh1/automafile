"""`dnd mv`, `dnd cp`, `dnd rm`, `dnd ls` — file operations that keep DB rows in sync."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from dragndoc.cli import app
from dragndoc.log import get_logger


log = get_logger(__name__)


def _resolve_move_target(src: Path, dst: Path, *, force: bool, allow_directory: bool) -> Path:
    """Validate ``src`` and resolve the destination for ``mv``/``cp``."""
    if not src.exists():
        typer.echo(f"Source not found: {src}", err=True)
        raise typer.Exit(1)
    if src.is_dir() and not allow_directory:
        typer.echo(f"Source is not a file: {src}", err=True)
        raise typer.Exit(1)
    if not (src.is_file() or src.is_dir()):
        typer.echo(f"Source is not a file: {src}", err=True)
        raise typer.Exit(1)

    target = dst / src.name if dst.exists() and dst.is_dir() else dst
    if target.resolve() == src.resolve():
        typer.echo(f"Source and destination are the same: {src}", err=True)
        raise typer.Exit(1)
    if src.is_dir() and target.exists():
        typer.echo(f"Target exists: {target}", err=True)
        raise typer.Exit(1)
    if src.is_dir() and target.resolve().is_relative_to(src.resolve()):
        typer.echo(f"Target is inside source directory: {target}", err=True)
        raise typer.Exit(1)
    if target.exists() and not force:
        typer.echo(f"Target exists: {target} (use -f to overwrite)", err=True)
        raise typer.Exit(1)

    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def _filesystem_entry_count(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return 1
    try:
        return 1 + sum(1 for _ in path.rglob("*"))
    except OSError:
        return 1


def _exact_doc_count(conn, rel: str) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM docs WHERE path = ?", (rel,)).fetchone()
    return int(row["n"])


def _exact_dir_count(conn, rel: str) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM dirs WHERE path = ?", (rel,)).fetchone()
    return int(row["n"])


def _planned_counts(rel: str, *, directory_scope: bool) -> tuple[int, int]:
    from dragndoc.db import connect
    from dragndoc.dirs import count_prefix

    with connect(readonly=True) as conn:
        if directory_scope:
            return count_prefix(conn, "docs", rel), count_prefix(conn, "dirs", rel)
        return _exact_doc_count(conn, rel), _exact_dir_count(conn, rel)


def _confirm_multi_change(
    action: str,
    *,
    docs_count: int,
    dirs_count: int,
    fs_count: int,
    yes: bool,
) -> None:
    if docs_count + dirs_count <= 1 and fs_count <= 1:
        return
    typer.echo(f"{action.capitalize()} affects {docs_count} docs, {dirs_count} dirs, and {fs_count} filesystem entries.")
    if yes:
        return
    if not typer.confirm("Continue?", default=False):
        raise typer.Exit(1)


@app.command()
def mv(
    src: Annotated[Path, typer.Argument(help="Source file path.")],
    dst: Annotated[Path, typer.Argument(help="Destination file path or directory.")],
    force: Annotated[bool, typer.Option("-f", "--force", help="Overwrite the target file if it exists.")] = False,
    yes: Annotated[bool, typer.Option("-y", "--yes", help="Skip confirmation for multi-change moves.")] = False,
) -> None:
    """Move a file or directory. Updates matching metadata paths."""
    import shutil as _shutil
    from dragndoc.db import transaction
    from dragndoc.dirs import rewrite_prefix
    from dragndoc.meta_store import relative_to_root

    target = _resolve_move_target(src, dst, force=force, allow_directory=True)
    src_is_dir = src.is_dir()
    src_rel = relative_to_root(src)
    docs_count, dirs_count = _planned_counts(src_rel, directory_scope=src_is_dir)
    fs_count = _filesystem_entry_count(src)
    _confirm_multi_change("move", docs_count=docs_count, dirs_count=dirs_count, fs_count=fs_count, yes=yes)

    log.info("CLI: mv %s -> %s (force=%s)", src, target, force)
    _shutil.move(str(src), str(target))
    new_rel = relative_to_root(target)
    with transaction() as conn:
        if src_is_dir:
            rewrite_prefix(conn, "dirs", src_rel, new_rel)
            rewrite_prefix(conn, "docs", src_rel, new_rel)
        else:
            conn.execute("UPDATE docs SET path = ? WHERE path = ?", (new_rel, src_rel))
    typer.echo(f"Moved: {target}")


@app.command()
def cp(
    src: Annotated[Path, typer.Argument(help="Source file path.")],
    dst: Annotated[Path, typer.Argument(help="Destination file path or directory.")],
    force: Annotated[bool, typer.Option("-f", "--force", help="Overwrite the target file if it exists.")] = False,
) -> None:
    """Copy a file. Duplicates the metadata row at the new path (same hash)."""
    import shutil as _shutil
    from dragndoc.meta_store import get_by_file, relative_to_root, upsert

    target = _resolve_move_target(src, dst, force=force, allow_directory=False)
    log.info("CLI: cp %s -> %s (force=%s)", src, target, force)
    _shutil.copy2(str(src), str(target))
    src_doc = get_by_file(src)
    if src_doc is not None:
        src_doc.id = None  # force INSERT path on upsert
        src_doc.path = relative_to_root(target)
        upsert(src_doc)
    typer.echo(f"Copied: {target}")


@app.command()
def rm(
    path: Annotated[Path, typer.Argument(help="File or directory path to remove.")],
    force: Annotated[bool, typer.Option("-f", "--force", help="Ignore missing file and exit successfully.")] = False,
    yes: Annotated[bool, typer.Option("-y", "--yes", help="Skip confirmation for multi-change removals.")] = False,
    metadata_only: Annotated[bool, typer.Option("--metadata-only", help="Only remove matching DB rows.")] = False,
) -> None:
    """Remove a file or directory and its metadata rows."""
    import shutil as _shutil
    from dragndoc.db import connect, transaction
    from dragndoc.dirs import count_prefix, delete_prefix
    from dragndoc.meta_store import recompute_dups_for_hashes, relative_to_root
    from dragndoc.paths import like_child_pattern

    exists = path.exists()
    rel = relative_to_root(path)
    with connect(readonly=True) as conn:
        prefix_dirs = count_prefix(conn, "dirs", rel)
        prefix_docs = count_prefix(conn, "docs", rel)

    if not exists and not (metadata_only and (prefix_dirs or prefix_docs)):
        if force:
            return
        typer.echo(f"Not found: {path}", err=True)
        raise typer.Exit(1)
    if exists and not (path.is_file() or path.is_dir()):
        typer.echo(f"Not a file or directory: {path}", err=True)
        raise typer.Exit(1)

    directory_scope = path.is_dir() if exists else prefix_dirs > 0
    docs_count, dirs_count = _planned_counts(rel, directory_scope=directory_scope)
    fs_count = 0 if metadata_only else _filesystem_entry_count(path)
    _confirm_multi_change("remove", docs_count=docs_count, dirs_count=dirs_count, fs_count=fs_count, yes=yes)

    log.info("CLI: rm %s (force=%s metadata_only=%s)", path, force, metadata_only)
    if not metadata_only:
        if path.is_dir():
            _shutil.rmtree(path)
        else:
            path.unlink()

    with transaction() as conn:
        if directory_scope:
            hash_rows = conn.execute(
                "SELECT hash FROM docs WHERE path = ? OR path LIKE ? ESCAPE '\\'",
                (rel, like_child_pattern(rel)),
            ).fetchall()
            delete_prefix(conn, "docs", rel)
            delete_prefix(conn, "dirs", rel)
        else:
            hash_rows = conn.execute("SELECT hash FROM docs WHERE path = ?", (rel,)).fetchall()
            conn.execute("DELETE FROM docs WHERE path = ?", (rel,))
            conn.execute("DELETE FROM dirs WHERE path = ?", (rel,))
    hashes = {row["hash"] for row in hash_rows if row["hash"]}
    if hashes:
        recompute_dups_for_hashes(hashes)
    typer.echo(f"Removed: {path}")


@app.command()
def ls(
    path: Annotated[Path, typer.Argument(help="Directory to list.")] = Path("."),
    show_all: Annotated[bool, typer.Option("-a", "--all", help="Show entries that start with a dot.")] = False,
) -> None:
    """List a directory; files with rows are marked, directories get mode tags."""
    from dragndoc.db import connect
    from dragndoc.dirs import auto_mode_for_path, mode_tag
    from dragndoc.meta_store import relative_to_root

    if not path.exists():
        typer.echo(f"Not found: {path}", err=True)
        raise typer.Exit(1)
    if not path.is_dir():
        typer.echo(f"Not a directory: {path}", err=True)
        raise typer.Exit(1)

    entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    dirs_for_query = [e for e in entries if e.is_dir() and (show_all or not e.name.startswith("."))]
    files_for_query = [e for e in entries if e.is_file() and (show_all or not e.name.startswith("."))]
    dir_rels = [relative_to_root(d) for d in dirs_for_query]
    rels = [relative_to_root(f) for f in files_for_query]
    rows: set[str] = set()
    dir_modes: dict[str, str] = {}
    with connect(readonly=True) as conn:
        if dir_rels:
            placeholders = ",".join("?" * len(dir_rels))
            for r in conn.execute(
                f"SELECT path, mode FROM dirs WHERE path IN ({placeholders})", dir_rels
            ).fetchall():
                dir_modes[r["path"]] = r["mode"]
        if rels:
            placeholders = ",".join("?" * len(rels))
            for r in conn.execute(
                f"SELECT path FROM docs WHERE path IN ({placeholders})", rels
            ).fetchall():
                rows.add(r["path"])

    rel_iter = iter(rels)
    dir_rel_iter = iter(dir_rels)
    for entry in entries:
        if not show_all and entry.name.startswith("."):
            continue
        if entry.is_dir():
            rel = next(dir_rel_iter)
            mode = dir_modes.get(rel) or auto_mode_for_path(entry)[0]
            typer.echo(f"[{mode_tag(mode)}] {entry.name}/")
        else:
            rel = next(rel_iter)
            mark = "*" if rel in rows else " "
            typer.echo(f"{mark} {entry.name}")
