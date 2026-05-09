"""`dnd grep` — FTS5 search over metadata."""

from __future__ import annotations

from typing import Annotated, Optional

import typer

from dragndoc.cli import app


_FTS_FIELDS = ("title", "summary", "notes", "tags", "parties")


@app.command()
def grep(
    pattern: Annotated[str, typer.Argument(help="FTS5 query: a word, phrase, or boolean expression (e.g. `tax AND receipt`).")],
    field: Annotated[Optional[str], typer.Option("--field", help=f"Restrict to one column: {', '.join(_FTS_FIELDS)}.")] = None,
    limit: Annotated[int, typer.Option("--limit", help="Max rows to return.")] = 50,
) -> None:
    """Search metadata with FTS5."""
    from dragndoc.db import connect

    if field is not None and field not in _FTS_FIELDS:
        typer.echo(f"--field must be one of: {', '.join(_FTS_FIELDS)}", err=True)
        raise typer.Exit(2)

    if field:
        # fts5 column scoping: prefix each query term with `colname:` would be
        # tedious for a free-form `pattern`; the `{col} : query` form scopes
        # the entire query to that column.
        match_query = f"{{{field}}} : {pattern}"
    else:
        match_query = pattern

    with connect(readonly=True) as conn:
        rows = conn.execute(
            "SELECT d.path, d.title, d.category "
            "FROM docs d JOIN docs_fts f ON d.id = f.rowid "
            "WHERE docs_fts MATCH ? "
            "ORDER BY bm25(docs_fts) "
            "LIMIT ?",
            (match_query, limit),
        ).fetchall()

    if not rows:
        typer.echo("(no matches)")
        return
    for r in rows:
        title = r["title"] or ""
        suffix = f" — {title}" if title else ""
        typer.echo(f"{r['path']} [{r['category']}]{suffix}")
