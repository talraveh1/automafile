"""`dnd meta` — inspect and edit document metadata rows."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Annotated

import typer

from dragndoc.cli import meta_app


_META_FRONTMATTER_FIELDS = {
    "category", "parties", "langs", "tags", "date", "title", "confidence", "summary", "notes", "dup",
}


def _require_doc(path: Path):
    """Look up the metadata row for ``path``; exit 1 with a consistent error if missing."""
    from dragndoc.meta_store import get_by_file

    doc = get_by_file(path)
    if doc is None:
        typer.echo(f"No row for: {path}", err=True)
        raise typer.Exit(1)
    return doc


def _freeze_identity(new_doc, base) -> None:
    """Force file-identity fields on ``new_doc`` to come from ``base`` (ignore frontmatter edits)."""
    new_doc.path = base.path
    new_doc.hash = base.hash
    new_doc.size = base.size
    new_doc.modified = base.modified
    new_doc.digested = base.digested
    new_doc.original = base.original
    new_doc.dup = base.dup


@meta_app.command("get")
def meta_get(
    paths: Annotated[list[Path], typer.Argument(help="One or more files, directories, or glob patterns. Looks up rows by relative path under the docs root.")],
    recursive: Annotated[bool, typer.Option("-r", "--recursive", help="When an argument is a directory, walk its whole subtree.")] = False,
    insensitive: Annotated[bool, typer.Option("-i", "--insensitive", help="Case-insensitive glob matching.")] = False,
) -> None:
    """JSON dump of one or more rows. Single match → single object; many → JSON array."""
    from dragndoc.cli._path_args import expand_paths

    expanded = expand_paths(paths, recursive=recursive, insensitive=insensitive)
    if not expanded:
        typer.echo(f"No matching files: {', '.join(str(p) for p in paths)}", err=True)
        raise typer.Exit(1)

    if len(expanded) == 1:
        doc = _require_doc(expanded[0])
        typer.echo(json.dumps(doc.to_dict(), indent=2, ensure_ascii=False, default=str))
        return

    from dragndoc.meta_store import get_by_file
    out: list[dict] = []
    for fp in expanded:
        doc = get_by_file(fp)
        if doc is None:
            continue
        out.append(doc.to_dict())
    typer.echo(json.dumps(out, indent=2, ensure_ascii=False, default=str))


@meta_app.command("cat")
def meta_cat(
    paths: Annotated[list[Path], typer.Argument(help="One or more files, directories, or glob patterns.")],
    recursive: Annotated[bool, typer.Option("-r", "--recursive", help="When an argument is a directory, walk its whole subtree.")] = False,
    insensitive: Annotated[bool, typer.Option("-i", "--insensitive", help="Case-insensitive glob matching.")] = False,
) -> None:
    """Markdown render of one or more rows (frontmatter + Summary + Notes)."""
    from dragndoc.cli._path_args import expand_paths
    from dragndoc.meta_store import get_by_file, to_markdown

    expanded = expand_paths(paths, recursive=recursive, insensitive=insensitive)
    if not expanded:
        typer.echo(f"No matching files: {', '.join(str(p) for p in paths)}", err=True)
        raise typer.Exit(1)

    if len(expanded) == 1:
        doc = _require_doc(expanded[0])
        typer.echo(to_markdown(doc), nl=False)
        return

    for i, fp in enumerate(expanded):
        doc = get_by_file(fp)
        if doc is None:
            continue
        if i:
            typer.echo("\n---\n")
        typer.echo(f"## {fp}\n", nl=False)
        typer.echo(to_markdown(doc), nl=False)


@meta_app.command("set")
def meta_set(
    path: Annotated[Path, typer.Argument(help="File path.")],
    assignments: Annotated[list[str], typer.Argument(help="One or more `field=value` pairs.")],
) -> None:
    """Set one or more fields on a row. `field=value`; lists comma-separated (e.g. `tags=tax,2025`)."""
    from dragndoc.meta_store import set_dup, upsert

    doc = _require_doc(path)
    dup_value: str | None = None

    for assignment in assignments:
        if "=" not in assignment:
            typer.echo(f"Bad assignment (expected field=value): {assignment}", err=True)
            raise typer.Exit(2)
        key, _, value = assignment.partition("=")
        key = key.strip()
        value = value.strip()
        if key not in _META_FRONTMATTER_FIELDS:
            typer.echo(f"Unknown or read-only field: {key}", err=True)
            raise typer.Exit(2)
        if key == "dup":
            dup_value = value
        elif key in {"parties", "langs", "tags"}:
            setattr(doc, key, [v.strip() for v in value.split(",") if v.strip()])
        elif key == "summary":
            doc.summary = value
        elif key == "notes":
            doc.notes = value
        else:
            setattr(doc, key, value or None)
    if any(not assignment.partition("=")[0].strip() == "dup" for assignment in assignments):
        upsert(doc)
    if dup_value is not None:
        try:
            set_dup(path, dup_value)
        except ValueError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(2) from None
    typer.echo(f"Updated: {doc.path}")


@meta_app.command("apply")
def meta_apply(
    path: Annotated[Path, typer.Argument(help="File path of the document.")],
    source: Annotated[Path, typer.Argument(help="Markdown file with YAML frontmatter to apply.")],
) -> None:
    """Whole-doc update from a markdown + frontmatter file."""
    from dragndoc.meta_store import doc_from_markdown, upsert

    base = _require_doc(path)
    if not source.exists():
        typer.echo(f"Source file not found: {source}", err=True)
        raise typer.Exit(1)

    text = source.read_text(encoding="utf-8")
    try:
        new_doc = doc_from_markdown(text, base=base)
    except ValueError as exc:
        typer.echo(f"Could not parse: {exc}", err=True)
        raise typer.Exit(2) from None
    _freeze_identity(new_doc, base)
    upsert(new_doc)
    typer.echo(f"Applied: {new_doc.path}")


@meta_app.command("edit")
def meta_edit(
    path: Annotated[Path, typer.Argument(help="File path of the document.")],
) -> None:
    """Open the row's markdown in $EDITOR; apply on save."""
    from dragndoc.meta_store import doc_from_markdown, to_markdown, upsert

    doc = _require_doc(path)

    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or ("notepad" if sys.platform == "win32" else "vi")
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
        f.write(to_markdown(doc))
        tmp_name = f.name

    try:
        subprocess.run([editor, tmp_name], check=False)
        edited = Path(tmp_name).read_text(encoding="utf-8")
        try:
            new_doc = doc_from_markdown(edited, base=doc)
        except ValueError as exc:
            typer.echo(f"Could not parse edited file (left at {tmp_name}): {exc}", err=True)
            raise typer.Exit(2) from None
        _freeze_identity(new_doc, doc)
        upsert(new_doc)
        typer.echo(f"Applied: {new_doc.path}")
    finally:
        try:
            Path(tmp_name).unlink()
        except OSError:
            pass
