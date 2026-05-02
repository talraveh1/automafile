"""Typer-based CLI for Automafile."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Optional

import typer

from automafile import __version__
from automafile.log import get_logger


log = get_logger(__name__)


app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Automafile — watch a folder, enrich files with metadata, file them via Claude.",
)


@app.callback()
def _root(version: Annotated[bool, typer.Option("--version", help="Print version and exit.")] = False) -> None:
    if version:
        typer.echo(f"automafile {__version__}")
        raise typer.Exit(0)


@app.command()
def watch(
    documents_root: Annotated[Optional[Path], typer.Option("--documents-root", help="Override DOCUMENTS_ROOT.")] = None,
) -> None:
    """Start the inbox watcher in the foreground."""
    if documents_root is not None:
        import os
        os.environ["DOCUMENTS_ROOT"] = str(documents_root.resolve())
        from automafile.config import reset_settings
        reset_settings()
    log.info("CLI: watch (documents_root=%s)", documents_root)
    from automafile.watcher import run_watcher
    run_watcher()


@app.command()
def process(
    path: Annotated[Optional[Path], typer.Argument(help="A file to process, a worklist JSON, or omitted to auto-pick the newest matching worklist.")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Run extraction + LLM but write nothing.")] = False,
    force_ocr: Annotated[bool, typer.Option("--force-ocr", help="Force OCR even if not recommended.")] = False,
    force: Annotated[bool, typer.Option("-f", "--force", help="Re-process worklist entries even if their `processed` mark is at-or-after the file's mtime.")] = False,
    stop_on_error: Annotated[bool, typer.Option("--stop-on-error", help="Worklist mode only: stop at the first failure.")] = False,
) -> None:
    """Process a worklist (default) or a single file."""
    from automafile.config import get_settings
    from automafile.pipeline import format_result_line, process_file

    settings = get_settings()
    if path is None or _looks_like_worklist(path, settings.scan_dir):
        _process_worklist(path, settings, dry_run=dry_run, force_ocr=force_ocr, force=force, stop_on_error=stop_on_error)
        return
    log.info("CLI: process %s (dry_run=%s force_ocr=%s)", path, dry_run, force_ocr)
    result = process_file(path, dry_run=dry_run, force_ocr=force_ocr)
    typer.echo(format_result_line(result))
    if result.error:
        raise typer.Exit(1)


def _looks_like_worklist(path: Path, scan_dir: Path) -> bool:
    if path.suffix.lower() != ".json":
        return False
    try:
        return path.resolve().parent == scan_dir.resolve()
    except OSError:
        return False


@app.command()
def ocr(
    path: Annotated[Path, typer.Argument(help="The file to OCR.")],
    langs: Annotated[Optional[str], typer.Option(help="Override TESSERACT_LANGS.")] = None,
) -> None:
    """Force OCR on a single file and print the recovered text."""
    log.info("CLI: ocr %s (langs=%s)", path, langs)
    from automafile.ocr import run_ocr
    text = run_ocr(path, langs=langs)
    typer.echo(text)


@app.command()
def scan(
    documents_root: Annotated[Optional[Path], typer.Option("--documents-root")] = None,
    path: Annotated[Optional[Path], typer.Option("--path", help="Limit the scan to a relative subpath under DOCUMENTS_ROOT (e.g. 'Inbox').")] = None,
    print_json: Annotated[bool, typer.Option("--json", help="Print the worklist instead of writing it.")] = False,
) -> None:
    """Run the scanner; emit a worklist JSON."""
    if documents_root is not None:
        import os
        os.environ["DOCUMENTS_ROOT"] = str(documents_root.resolve())
        from automafile.config import reset_settings
        reset_settings()
    log.info("CLI: scan (path=%s, json=%s)", path, print_json)
    from automafile.scanner import run_scan, write_worklist
    wl = run_scan(subpath=path)
    if print_json:
        typer.echo(json.dumps(wl.to_dict(), indent=2, ensure_ascii=False))
        return
    out = write_worklist(wl)
    if out is None:
        typer.echo(f"scan complete: seen={wl.files_seen}; everything is already in an existing worklist.")
        typer.echo("next: automafile process")
        return
    typer.echo(
        f"scan complete: seen={wl.files_seen} need_ocr={len(wl.files_needing_ocr)} "
        f"need_meta={len(wl.files_needing_metadata)} partial={len(wl.files_with_partial_metadata)} "
        f"stale={len(wl.files_with_stale_metadata)} ocr_review={len(wl.ocr_review_candidates)} "
        f"orphans={len(wl.orphan_sidecars)} quarantined={len(wl.quarantined_sidecars)} "
        f"unprocessable={len(wl.unprocessable_files)} (counts reflect new entries only)"
    )
    typer.echo(f"worklist: {out}")
    typer.echo(f"next: automafile process")


_WORKLIST_BUCKETS = (
    "files_needing_ocr",
    "files_needing_metadata",
    "files_with_partial_metadata",
    "files_with_stale_metadata",
)


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write ``data`` to ``path`` durably: temp file → flush → fsync → rename.

    Used to update worklists after each processed file so a crash mid-run
    doesn't lose the marks already made.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _utc_iso_now() -> str:
    # microseconds, not seconds: file mtimes carry sub-second precision on
    # most filesystems, so a seconds-resolution mark can compare as "older
    # than the file" within the same second and trigger spurious re-runs.
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _is_already_processed(rel: str, refs: list, documents_root: Path) -> bool:
    """True if ``rel`` has been marked ``processed`` and its file mtime hasn't moved past that mark.

    ``refs`` is a list of ``(source_path, bucket_name, entry_dict)`` tuples;
    we take the most-recent ``processed`` timestamp across them and compare
    to the file's current mtime.
    """
    file_path = documents_root / rel
    if not file_path.exists():
        return False
    try:
        file_mtime = datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return False
    latest: datetime | None = None
    for _, _, entry in refs:
        ts = entry.get("processed")
        if not ts:
            continue
        try:
            t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        if latest is None or t > latest:
            latest = t
    if latest is None:
        return False
    return file_mtime <= latest


def _mark_processed(refs: list, source_data: dict, ts: str) -> set[Path]:
    """Stamp every entry that referenced this rel with ``processed = ts``.

    Returns the set of source paths that were touched, so the caller knows
    which files to rewrite.
    """
    touched: set[Path] = set()
    for src_path, _, entry in refs:
        entry["processed"] = ts
        touched.add(src_path)
    return touched


def _process_worklist(
    worklist: Optional[Path],
    settings,
    *,
    dry_run: bool,
    force_ocr: bool,
    force: bool,
    stop_on_error: bool,
) -> None:
    from automafile.pipeline import format_result_line, process_file

    log.info(
        "CLI: process worklist (worklist=%s dry_run=%s force_ocr=%s force=%s)",
        worklist, dry_run, force_ocr, force,
    )
    expected_root = str(settings.documents_root)
    explicit = worklist is not None
    sources: list[Path]
    if explicit:
        sources = [worklist]
    else:
        # Sweep every candidate: remove unreadable ones and worklists whose
        # documents_root doesn't match (left over from a different host or
        # test run); merge what's left.
        sources = []
        for cand in sorted(settings.scan_dir.glob("scan-*.json")):
            try:
                cand_root = json.loads(cand.read_text(encoding="utf-8")).get("documents_root")
            except (OSError, json.JSONDecodeError) as exc:
                log.warning("process: removing unreadable worklist %s (%s)", cand.name, exc)
                cand.unlink(missing_ok=True)
                continue
            if cand_root == expected_root:
                sources.append(cand)
            else:
                log.info("process: removing stale worklist %s (root=%s)", cand.name, cand_root)
                cand.unlink(missing_ok=True)
        if not sources:
            typer.echo(f"no worklist found in {settings.scan_dir} for {expected_root}", err=True)
            raise typer.Exit(2)
        if len(sources) == 1:
            typer.echo(f"using worklist: {sources[0]}")
        else:
            typer.echo(f"merging {len(sources)} worklists for {expected_root}")

    # Load each source once; build entries_by_rel so a single rel can be
    # marked across every worklist that references it (we may merge several).
    source_data: dict[Path, dict] = {}
    entries_by_rel: dict[str, list[tuple[Path, str, dict]]] = {}
    for src in sources:
        data = json.loads(src.read_text(encoding="utf-8"))
        wl_root = data.get("documents_root")
        if wl_root != expected_root:
            typer.echo(
                f"{src.name} was scanned for {wl_root}, but DOCUMENTS_ROOT is {expected_root}",
                err=True,
            )
            log.error("process: %s root mismatch (worklist=%s, current=%s)", src.name, wl_root, expected_root)
            raise typer.Exit(2)
        source_data[src] = data
        for bucket in _WORKLIST_BUCKETS:
            for entry in data.get(bucket, []):
                rel = entry.get("relative_path")
                if rel:
                    entries_by_rel.setdefault(rel, []).append((src, bucket, entry))

    # Decide what to do: keep order-of-discovery; drop already-processed
    # unless --force; dedup so the same rel doesn't run twice in one invocation.
    todo: list[str] = []
    skipped = 0
    seen: set[str] = set()
    for src in sources:
        data = source_data[src]
        for bucket in _WORKLIST_BUCKETS:
            for entry in data.get(bucket, []):
                rel = entry.get("relative_path")
                if not rel or rel in seen:
                    continue
                seen.add(rel)
                refs = entries_by_rel[rel]
                if not force and _is_already_processed(rel, refs, settings.documents_root):
                    skipped += 1
                    continue
                todo.append(rel)

    label = sources[0].name if len(sources) == 1 else f"{len(sources)} worklists"

    if not todo:
        msg = f"nothing to process in {label}"
        if skipped:
            msg += f" ({skipped} already-processed entr{'y' if skipped == 1 else 'ies'} skipped; use --force to redo)"
        typer.echo(msg)
        log.info("process: nothing to process across %d worklist(s) (skipped=%d)", len(sources), skipped)
        if not explicit:
            for src in sources:
                src.unlink(missing_ok=True)
        return

    msg = f"processing {len(todo)} file(s) from {label}"
    if skipped:
        msg += f" ({skipped} already-processed skipped; use --force to redo)"
    typer.echo(msg)
    log.info("process: %d file(s) from %d worklist(s) (skipped=%d)", len(todo), len(sources), skipped)

    failures = 0
    for rel in todo:
        path = settings.documents_root / rel
        if not path.exists():
            typer.echo(f"{rel} | MISSING under {settings.documents_root}")
            log.error("process: %s missing under %s", rel, settings.documents_root)
            failures += 1
            if stop_on_error:
                raise typer.Exit(1)
            continue
        result = process_file(path, dry_run=dry_run, force_ocr=force_ocr)
        typer.echo(format_result_line(result))
        if result.error:
            failures += 1
            if stop_on_error:
                raise typer.Exit(1)
            continue
        if dry_run:
            # dry-run did no real work; don't pollute the worklist with marks.
            continue
        # Mark every reference to this rel and durably rewrite each touched source.
        touched = _mark_processed(entries_by_rel[rel], source_data, _utc_iso_now())
        for src_path in touched:
            try:
                _atomic_write_json(src_path, source_data[src_path])
            except OSError as exc:
                log.warning("process: could not rewrite %s after marking %s: %s", src_path, rel, exc)

    typer.echo(f"done: {len(todo) - failures}/{len(todo)} succeeded")
    log.info("process done: %d/%d succeeded", len(todo) - failures, len(todo))
    if not explicit and failures == 0:
        for src in sources:
            src.unlink(missing_ok=True)
        log.info("process: removed %d drained worklist(s)", len(sources))
    if failures:
        raise typer.Exit(1)


@app.command("review-ocr")
def review_ocr(
    yes_all: Annotated[bool, typer.Option("--yes-all", help="Re-OCR every candidate without asking.")] = False,
) -> None:
    """Walk OCR review candidates and act on each."""
    from automafile.config import get_settings
    from automafile.metadata.sidecar import read as sidecar_read, write as sidecar_write
    from automafile.metadata.schema import utc_now_iso
    from automafile.ocr import run_ocr, tesseract_version
    from automafile.scanner import run_scan

    log.info("CLI: review-ocr (yes_all=%s)", yes_all)
    settings = get_settings()
    wl = run_scan()
    if not wl.ocr_review_candidates:
        typer.echo("No OCR review candidates.")
        return

    for entry in wl.ocr_review_candidates:
        rel = entry["relative_path"]
        path = settings.documents_root / rel
        typer.echo(f"\nCandidate: {rel}")
        typer.echo(f"  previous: {entry.get('previous_engine')} / {entry.get('previous_languages')}")
        typer.echo(f"  current : {entry.get('current_engine')} / {entry.get('current_languages')}")
        choice = "y" if yes_all else typer.prompt("Re-OCR? [y/N/skip]", default="N")
        if choice.lower().startswith("y"):
            try:
                text = run_ocr(path)
                doc, summary, notes = sidecar_read(path)
                if doc is not None:
                    doc.ocr.engine = "tesseract"
                    doc.ocr.engine_version = tesseract_version()
                    doc.ocr.languages = settings.tesseract_langs
                    doc.ocr.done_at = utc_now_iso()
                    doc.metadata_modified = utc_now_iso()
                    sidecar_write(path, doc, summary or text[:1000], notes)
                typer.echo(f"  re-OCR'd ({len(text)} chars).")
            except Exception as exc:  # noqa: BLE001
                typer.echo(f"  failed: {exc}")
        elif choice.lower().startswith("s"):
            typer.echo("  skipped (will reappear on next scan)")
        else:
            doc, summary, notes = sidecar_read(path)
            if doc is not None:
                doc.metadata_modified = utc_now_iso()
                sidecar_write(path, doc, summary, notes)
            typer.echo("  declined; metadata_modified bumped.")


@app.command()
def inspect(
    path: Annotated[Optional[Path], typer.Argument(help="A file or directory. Omit to walk the inbox.")] = None,
    recursive: Annotated[bool, typer.Option("--recursive/--no-recursive", help="Walk directories recursively.")] = True,
) -> None:
    """Dump sidecar metadata for one or many files as JSON. Read-only — no extraction, no LLM, no writes."""
    from automafile.config import get_settings

    settings = get_settings()
    target = path if path is not None else settings.inbox_path

    if not target.exists():
        typer.echo(f"path not found: {target}", err=True)
        raise typer.Exit(1)

    paths = _collect_inspect_paths(target, recursive)
    log.info("CLI: inspect %s (files=%d, recursive=%s)", target, len(paths), recursive)
    out = [_inspect_one(p, settings.documents_root) for p in paths]
    typer.echo(json.dumps(out, indent=2, ensure_ascii=False, default=str))


def _collect_inspect_paths(start: Path, recursive: bool) -> list[Path]:
    if start.is_file():
        return [start]
    iterator = start.rglob("*") if recursive else start.iterdir()
    paths: list[Path] = []
    for p in sorted(iterator):
        if not p.is_file():
            continue
        try:
            rel_parts = p.relative_to(start).parts
        except ValueError:
            rel_parts = ()
        if any(part.startswith(".") for part in rel_parts):
            continue
        paths.append(p)
    return paths


def _inspect_one(file_path: Path, documents_root: Path) -> dict:
    from automafile.metadata.sidecar import read as sidecar_read

    try:
        rel = str(file_path.relative_to(documents_root)).replace("\\", "/")
    except ValueError:
        rel = str(file_path).replace("\\", "/")

    try:
        doc, summary, notes = sidecar_read(file_path)
    except Exception as exc:
        doc, summary, notes = None, "", ""
        log.debug("sidecar read failed for %s: %s", file_path, exc)

    if doc is not None:
        return {
            "relative_path": rel,
            "has_sidecar": True,
            "metadata": doc.to_frontmatter_dict(),
            "summary": summary,
            "notes": notes,
        }
    return {
        "relative_path": rel,
        "has_sidecar": False,
        "metadata": None,
        "summary": None,
        "notes": None,
    }


@app.command()
def mv(
    src: Annotated[Path, typer.Argument(help="Source file path.")],
    dst: Annotated[Path, typer.Argument(help="Destination file path or directory.")],
    force: Annotated[bool, typer.Option("-f", "--force", help="Overwrite target file and target sidecar if they exist.")] = False,
) -> None:
    """Move a file together with its sidecar. Fails if target file or target sidecar exists, unless ``-f``."""
    import shutil
    from automafile.metadata.sidecar import sidecar_path_for, update_relative_path

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

    src_sidecar = sidecar_path_for(src)
    target_sidecar = sidecar_path_for(target)

    if target.exists() and not force:
        typer.echo(f"target exists: {target} (use -f to overwrite)", err=True)
        raise typer.Exit(1)
    if target_sidecar.exists() and not force:
        typer.echo(f"target sidecar exists: {target_sidecar} (use -f to overwrite)", err=True)
        raise typer.Exit(1)

    target.parent.mkdir(parents=True, exist_ok=True)
    log.info("CLI: mv %s -> %s (force=%s)", src, target, force)
    shutil.move(str(src), str(target))
    if src_sidecar.exists():
        update_relative_path(src, target)
    typer.echo(f"moved: {target}")


@app.command()
def reconcile(
    yes_all: Annotated[bool, typer.Option("--yes-all", help="Auto-accept hash-matched relinks when there is a single match.")] = False,
) -> None:
    """Walk orphan sidecars and propose hash-matched relinks."""
    from automafile.config import get_settings
    from automafile.metadata.reconcile import find_orphans
    from automafile.metadata.sidecar import update_relative_path, sidecar_path_for

    log.info("CLI: reconcile (yes_all=%s)", yes_all)
    settings = get_settings()
    orphans = find_orphans(settings.documents_root)
    if not orphans:
        typer.echo("No orphan sidecars.")
        return

    for orphan in orphans:
        typer.echo(f"\nOrphan: {orphan.sidecar_path}")
        typer.echo(f"  described file: {orphan.described_relative_path}")
        if not orphan.matches_in_tree:
            typer.echo("  no hash matches; leave for manual cleanup.")
            continue
        for i, p in enumerate(orphan.matches_in_tree):
            typer.echo(f"  [{i}] {p}")
        if yes_all and len(orphan.matches_in_tree) == 1:
            choice = "0"
        else:
            choice = typer.prompt("Pick index to relink (or 'n' to skip)", default="n")
        if choice.lower().startswith("n"):
            continue
        try:
            idx = int(choice)
            target = orphan.matches_in_tree[idx]
        except (ValueError, IndexError):
            typer.echo("  invalid choice; skipping.")
            continue
        # the orphan's described filename + sidecar location implies the original path
        # described as <sidecar.parent.parent>/<filename>; fabricate a stand-in path
        original = orphan.sidecar_path.parent.parent / orphan.described_filename
        update_relative_path(original, target)
        typer.echo(f"  relinked to {target}")


@app.command()
def bootstrap(
    force: Annotated[bool, typer.Option("--force", help="Overwrite memory templates even if present.")] = False,
) -> None:
    """Seed memory templates and create folder layout. Idempotent."""
    log.info("CLI: bootstrap (force=%s)", force)
    from automafile.bootstrap import bootstrap as bootstrap_fn
    bootstrap_fn(force=force)


@app.command()
def doctor() -> None:
    """Diagnose the local environment (Tesseract, Ollama, paths)."""
    log.info("CLI: doctor")
    from automafile.config import get_settings
    from automafile.llm import ollama_available, ollama_has_model
    from automafile.ocr import tesseract_available, tesseract_languages, tesseract_version

    settings = get_settings()
    typer.echo(f"Documents root: {settings.documents_root}{'  (exists)' if settings.documents_root.exists() else '  (missing)'}")
    typer.echo(f"  inbox: {settings.inbox_path}{'  (exists)' if settings.inbox_path.exists() else '  (missing)'}")

    tess = tesseract_available()
    typer.echo(f"Tesseract present: {tess}")
    if tess:
        typer.echo(f"  version  : {tesseract_version()}")
        typer.echo(f"  langs    : {', '.join(tesseract_languages()) or '(unknown)'}")

    available = ollama_available()
    typer.echo(f"Ollama reachable : {available}  ({settings.ollama_url})")
    if available:
        typer.echo(f"  model present : {ollama_has_model()}  ({settings.ollama_model})")


@app.command("filer-apply")
def filer_apply_cmd(
    path: Annotated[Path, typer.Argument(help="The file to file.")],
    category: Annotated[str, typer.Option(help="Top-level category.")] = "Unknown",
    subcategory: Annotated[Optional[str], typer.Option(help="Optional subcategory.")] = None,
    name: Annotated[Optional[str], typer.Option(help="Smart filename. Auto-derived if omitted.")] = None,
    overwrite: Annotated[bool, typer.Option("--overwrite", help="Overwrite the target if it exists with different content.")] = False,
) -> None:
    r"""Move ``path`` into the managed tree under ``category\[/subcategory]/<name>``."""
    log.info("CLI: filer-apply %s (category=%s subcategory=%s overwrite=%s)", path, category, subcategory, overwrite)
    from automafile.filer import FilingProposal, apply_filing, smart_filename
    from automafile.metadata.sidecar import read as sidecar_read

    if name is None:
        doc, summary, _ = sidecar_read(path)
        meta = {}
        if doc is not None:
            meta = {
                "title": doc.title,
                "summary": summary or "",
                "correspondent": doc.correspondent,
                "date": doc.date,
                "category": category,
                "subcategory": subcategory,
            }
        meta["extension"] = path.suffix.lstrip(".")
        name = smart_filename(meta, path.suffix.lstrip("."))

    proposal = FilingProposal(category=category, subcategory=subcategory, smart_name=name)
    target = apply_filing(path, proposal, overwrite=overwrite)
    typer.echo(f"filed: {target}")


def main() -> None:  # pragma: no cover
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
