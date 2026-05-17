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


def _split_sources_and_target(args: list[Path]) -> tuple[list[Path], Path]:
    """``mv a b c destdir/`` → ``([a,b,c], destdir/)``. The last arg is the target.

    Validates: there must be ≥ 2 args; when 3+ args, the target must be a directory
    (mirrors `mv`/`cp` Linux semantics). Returns (sources, target).
    """
    if len(args) < 2:
        typer.echo("Expected at least one source and a target.", err=True)
        raise typer.Exit(2)
    *sources, target = args
    if len(sources) >= 2 and not (target.exists() and target.is_dir()):
        typer.echo(
            f"With multiple sources, the target must be an existing directory: {target}",
            err=True,
        )
        raise typer.Exit(2)
    return sources, target


def _is_glob(arg: Path) -> bool:
    """For mv/cp: treat a source as a glob only when it contains glob characters.

    Directories should NOT be auto-expanded into their contents — that breaks
    the Linux `mv src_dir dst_dir` semantic (which renames the dir, not its
    children). Globs (``*.mp3``, ``**/foo``) get expanded; plain paths don't.
    """
    s = str(arg)
    return any(c in s for c in ("*", "?", "["))


@app.command()
def mv(
    args: Annotated[list[Path], typer.Argument(help="Source(s) and target. With 2+ sources, the target must be an existing directory.")],
    force: Annotated[bool, typer.Option("-f", "--force", help="Overwrite the target file if it exists.")] = False,
    yes: Annotated[bool, typer.Option("-y", "--yes", help="Skip confirmation for multi-change moves.")] = False,
    no_sidecars: Annotated[bool, typer.Option("--no-sidecars", help="Don't move <base>.srt or other recognized sidecars alongside the file.")] = False,
    recursive: Annotated[bool, typer.Option("-r", "--recursive", help="When a source is a directory glob, walk its whole subtree.")] = False,
    insensitive: Annotated[bool, typer.Option("-i", "--insensitive", help="Case-insensitive glob matching for sources.")] = False,
) -> None:
    """Move file(s) or a directory. Updates metadata paths and follows recognized sidecars.

    Supports `mv src dst` (rename) and `mv s1 s2 s3 destdir/` (move many into a dir).
    Sources can be file paths, directory paths, or glob patterns (e.g. `Inbox/voix/**/*.mp3`).
    """
    from dragndoc.cli._path_args import expand_paths

    sources, target = _split_sources_and_target(args)

    # expand only GLOB sources; plain files and plain directories pass through as-is
    # (mv/cp on a directory should rename the directory, not iterate its children)
    expanded_sources: list[Path] = []
    for s in sources:
        if _is_glob(s):
            expanded_sources.extend(expand_paths([s], recursive=recursive, insensitive=insensitive))
        else:
            expanded_sources.append(s)

    if not expanded_sources:
        typer.echo("No source files matched.", err=True)
        raise typer.Exit(1)

    multi = len(expanded_sources) > 1
    if multi and not (target.exists() and target.is_dir()):
        typer.echo(
            f"Glob/directory expansion produced {len(expanded_sources)} sources; "
            f"target must be an existing directory: {target}",
            err=True,
        )
        raise typer.Exit(2)

    if multi and not yes:
        typer.echo(f"About to move {len(expanded_sources)} files into {target}:")
        for s in expanded_sources[:10]:
            typer.echo(f"  {s}")
        if len(expanded_sources) > 10:
            typer.echo(f"  ... and {len(expanded_sources) - 10} more")
        if not typer.confirm("Continue?", default=False):
            raise typer.Exit(1)

    for src in expanded_sources:
        try:
            _mv_one(src, target, force=force, yes=True, no_sidecars=no_sidecars)
        except typer.Exit:
            if not multi:
                raise


def _mv_one(
    src: Path,
    dst: Path,
    *,
    force: bool,
    yes: bool,
    no_sidecars: bool,
) -> None:
    """Single-source mv; the original mv body wrapped to be reusable."""
    import shutil as _shutil
    from dragndoc import asr_artifacts
    from dragndoc.db import transaction
    from dragndoc.dirs import rewrite_prefix
    from dragndoc.meta_store import relative_to_root

    target = _resolve_move_target(src, dst, force=force, allow_directory=True)
    src_is_dir = src.is_dir()
    src_rel = relative_to_root(src)
    docs_count, dirs_count = _planned_counts(src_rel, directory_scope=src_is_dir)
    sidecars_before = [] if (src_is_dir or no_sidecars) else asr_artifacts.sidecar_paths_for(src)
    fs_count = _filesystem_entry_count(src) + len(sidecars_before)
    _confirm_multi_change("move", docs_count=docs_count, dirs_count=dirs_count, fs_count=fs_count, yes=yes)

    log.info("CLI: mv %s -> %s (force=%s no_sidecars=%s)", src, target, force, no_sidecars)
    _shutil.move(str(src), str(target))
    new_rel = relative_to_root(target)
    moved_sidecars: list[tuple[Path, Path]] = []
    if not src_is_dir and not no_sidecars:
        moved_sidecars = asr_artifacts.follow_move(src, target)
    with transaction() as conn:
        if src_is_dir:
            rewrite_prefix(conn, "dirs", src_rel, new_rel)
            rewrite_prefix(conn, "docs", src_rel, new_rel)
        else:
            conn.execute("UPDATE docs SET path = ? WHERE path = ?", (new_rel, src_rel))
            for old, new in moved_sidecars:
                if old.suffix == asr_artifacts.SRT_SUFFIX:
                    new_srt_rel = relative_to_root(new)
                    conn.execute(
                        "UPDATE asr SET srt_path = ? "
                        "WHERE doc_id = (SELECT id FROM docs WHERE path = ?)",
                        (new_srt_rel, new_rel),
                    )
    msg = f"Moved: {target}"
    if moved_sidecars:
        msg += f" (+ {len(moved_sidecars)} sidecar{'s' if len(moved_sidecars) != 1 else ''})"
    typer.echo(msg)


@app.command()
def cp(
    args: Annotated[list[Path], typer.Argument(help="Source(s) and target. With 2+ sources, the target must be an existing directory.")],
    force: Annotated[bool, typer.Option("-f", "--force", help="Overwrite the target file if it exists.")] = False,
    no_sidecars: Annotated[bool, typer.Option("--no-sidecars", help="Don't copy <base>.srt or other recognized sidecars alongside the file.")] = False,
    recursive: Annotated[bool, typer.Option("-r", "--recursive", help="When a source is a directory glob, walk its whole subtree.")] = False,
    insensitive: Annotated[bool, typer.Option("-i", "--insensitive", help="Case-insensitive glob matching for sources.")] = False,
) -> None:
    """Copy file(s). Duplicates metadata rows at new paths; follows sidecars.

    Supports `cp src dst` (rename copy) and `cp s1 s2 s3 destdir/` (copy many into a dir).
    Sources can be file paths, directory paths, or glob patterns.
    """
    from dragndoc.cli._path_args import expand_paths, is_pattern_arg

    sources, target = _split_sources_and_target(args)

    expanded_sources: list[Path] = []
    for s in sources:
        if is_pattern_arg(s):
            expanded_sources.extend(expand_paths([s], recursive=recursive, insensitive=insensitive))
        else:
            expanded_sources.append(s)

    if not expanded_sources:
        typer.echo("No source files matched.", err=True)
        raise typer.Exit(1)

    multi = len(expanded_sources) > 1
    if multi and not (target.exists() and target.is_dir()):
        typer.echo(
            f"Glob/directory expansion produced {len(expanded_sources)} sources; "
            f"target must be an existing directory: {target}",
            err=True,
        )
        raise typer.Exit(2)

    for src in expanded_sources:
        try:
            _cp_one(src, target, force=force, no_sidecars=no_sidecars)
        except typer.Exit:
            if not multi:
                raise


def _cp_one(src: Path, dst: Path, *, force: bool, no_sidecars: bool) -> None:
    """Single-source cp; the original cp body wrapped to be reusable."""
    import shutil as _shutil
    from dragndoc import asr_artifacts
    from dragndoc.meta_store import get_by_file, relative_to_root, upsert

    target = _resolve_move_target(src, dst, force=force, allow_directory=False)
    log.info("CLI: cp %s -> %s (force=%s no_sidecars=%s)", src, target, force, no_sidecars)
    _shutil.copy2(str(src), str(target))
    copied_sidecars: list[tuple[Path, Path]] = []
    if not no_sidecars:
        copied_sidecars = asr_artifacts.follow_copy(src, target)
    src_doc = get_by_file(src)
    if src_doc is not None:
        src_doc.id = None
        src_doc.path = relative_to_root(target)
        for old, new in copied_sidecars:
            if old.suffix == asr_artifacts.SRT_SUFFIX:
                src_doc.asr.srt_path = relative_to_root(new)
        upsert(src_doc)
    msg = f"Copied: {target}"
    if copied_sidecars:
        msg += f" (+ {len(copied_sidecars)} sidecar{'s' if len(copied_sidecars) != 1 else ''})"
    typer.echo(msg)


@app.command()
def rm(
    paths: Annotated[list[Path], typer.Argument(help="One or more files, directories, or glob patterns to remove.")],
    force: Annotated[bool, typer.Option("-f", "--force", help="Ignore missing files and exit successfully.")] = False,
    yes: Annotated[bool, typer.Option("-y", "--yes", help="Skip confirmation for multi-change removals.")] = False,
    purge: Annotated[bool, typer.Option("-P", "--purge", help="Permanently delete instead of moving to the OS recycle bin.")] = False,
    metadata_only: Annotated[bool, typer.Option("--metadata-only", help="Only remove matching DB rows.")] = False,
    no_sidecars: Annotated[bool, typer.Option("--no-sidecars", help="Don't remove <base>.srt or other recognized sidecars alongside the file.")] = False,
    recursive: Annotated[bool, typer.Option("-r", "--recursive", help="When an argument is a directory, walk its whole subtree.")] = False,
    insensitive: Annotated[bool, typer.Option("-i", "--insensitive", help="Case-insensitive glob matching.")] = False,
) -> None:
    """Remove one or more files/directories and their metadata rows. Follows recognized sidecars.

    By default the filesystem entries are moved to the OS recycle bin so a
    mistake is recoverable; pass ``-P``/``--purge`` to delete permanently.
    """
    from dragndoc.cli._path_args import expand_paths

    # Only globs get auto-expanded into multiple paths. A plain directory arg
    # passes through as a single removal (the legacy `dnd rm <dir>` behavior:
    # remove the whole tree atomically, not file-by-file).
    has_glob = any(_is_glob(p) for p in paths)
    if has_glob or len(paths) > 1:
        targets: list[Path] = []
        for p in paths:
            if _is_glob(p):
                targets.extend(expand_paths([p], recursive=recursive, insensitive=insensitive, must_exist=False))
            else:
                targets.append(p)
        # dedupe preserving order
        seen: set[str] = set()
        deduped: list[Path] = []
        for t in targets:
            k = str(t)
            if k in seen:
                continue
            seen.add(k)
            deduped.append(t)
        targets = deduped
        if not targets:
            if force:
                return
            typer.echo(f"No matching files: {', '.join(str(p) for p in paths)}", err=True)
            raise typer.Exit(1)
        # bulk confirmation
        if not yes and len(targets) > 1:
            typer.echo(f"About to remove {len(targets)} files / directories:")
            for t in targets[:10]:
                typer.echo(f"  {t}")
            if len(targets) > 10:
                typer.echo(f"  ... and {len(targets) - 10} more")
            if not typer.confirm("Continue?", default=False):
                raise typer.Exit(1)
        for t in targets:
            try:
                _rm_one(t, force=force, yes=True, purge=purge, metadata_only=metadata_only, no_sidecars=no_sidecars)
            except typer.Exit:
                # individual failures don't abort the batch (force=True can also pre-empt)
                continue
        return

    _rm_one(paths[0], force=force, yes=yes, purge=purge, metadata_only=metadata_only, no_sidecars=no_sidecars)


def _rm_one(
    path: Path,
    *,
    force: bool,
    yes: bool,
    purge: bool,
    metadata_only: bool,
    no_sidecars: bool,
) -> None:
    """Single-path removal: original `dnd rm` semantics. Follows sidecars."""
    import shutil as _shutil
    from send2trash import send2trash
    from dragndoc import asr_artifacts
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
    sidecars_before: list[Path] = []
    if not metadata_only and not directory_scope and exists and path.is_file() and not no_sidecars:
        sidecars_before = asr_artifacts.sidecar_paths_for(path)
    fs_count = (0 if metadata_only else _filesystem_entry_count(path)) + len(sidecars_before)
    _confirm_multi_change("remove", docs_count=docs_count, dirs_count=dirs_count, fs_count=fs_count, yes=yes)

    log.info(
        "CLI: rm %s (force=%s purge=%s metadata_only=%s no_sidecars=%s)",
        path, force, purge, metadata_only, no_sidecars,
    )
    deleted_sidecars: list[Path] = []
    deleted_json_twins: list[int] = []
    if not metadata_only:
        if not no_sidecars and not directory_scope and exists and path.is_file():
            deleted_sidecars = asr_artifacts.follow_rm(path, purge=purge)
        try:
            if purge:
                if path.is_dir():
                    _shutil.rmtree(path)
                else:
                    path.unlink()
            else:
                send2trash(str(path))
        except OSError as exc:
            kind = "permanent delete" if purge else "recycle-bin move"
            typer.echo(f"{kind} failed for {path}: {exc}", err=True)
            log.error("rm %s failed: %s", path, exc)
            raise typer.Exit(1) from exc

    with transaction() as conn:
        # capture doc ids before delete so we can clean up JSON twins
        if directory_scope:
            id_rows = conn.execute(
                "SELECT id FROM docs WHERE path = ? OR path LIKE ? ESCAPE '\\'",
                (rel, like_child_pattern(rel)),
            ).fetchall()
        else:
            id_rows = conn.execute("SELECT id FROM docs WHERE path = ?", (rel,)).fetchall()
        deleted_doc_ids = [int(r["id"]) for r in id_rows]
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
    # clean up the per-doc JSON twin sidecar (lives under data/transcripts/)
    if not metadata_only:
        for doc_id in deleted_doc_ids:
            if asr_artifacts.delete_json_twin(doc_id, purge=purge):
                deleted_json_twins.append(doc_id)
    msg = f"Removed: {path}"
    extras: list[str] = []
    if deleted_sidecars:
        extras.append(f"{len(deleted_sidecars)} sidecar{'s' if len(deleted_sidecars) != 1 else ''}")
    if deleted_json_twins:
        extras.append(f"{len(deleted_json_twins)} JSON twin{'s' if len(deleted_json_twins) != 1 else ''}")
    if extras:
        msg += " (+ " + ", ".join(extras) + ")"
    typer.echo(msg)


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
