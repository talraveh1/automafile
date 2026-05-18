"""`dnd mux` — remux audio files into player-friendly MKV containers.

Per-file flow: Opus-encode the audio, mux it with the ``.srt`` sidecar
(if present) and a tiny dummy black video into a sibling ``.mkv``,
restore ``mtime``/``ctime`` to the original's, and update the docs
row's ``path``/``hash``/``size``. The ``.srt`` sidecar stays in place;
the source audio is removed unless ``--keep-original`` is set.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from dragndoc.cli import app
from dragndoc.log import get_logger


log = get_logger(__name__)


# audio extensions `dnd mux` will process; mirrors AUDIO_EXT in the scanner
_AUDIO_EXTS = {".amr", ".mp3", ".m4a", ".wav", ".ogg", ".opus", ".flac", ".aac"}


def _filter_audio(paths: list[Path]) -> list[Path]:
    return [p for p in paths if p.suffix.lower() in _AUDIO_EXTS]


def _resolve_language(doc, default_lang: str) -> str:
    """Prefer the asr-detected language; fall back to the configured default."""
    if doc is None or doc.asr is None:
        return default_lang
    detected = (doc.asr.detected_lang or "").strip().lower()
    if not detected:
        return default_lang
    # iso-639-2 three-letter codes Matroska expects ("heb", "eng", …);
    # whisper emits two-letter codes ("he", "en"), so map the common ones
    _two_to_three = {"he": "heb", "en": "eng", "ar": "ara", "ru": "rus", "fr": "fra", "es": "spa"}
    return _two_to_three.get(detected, detected if len(detected) == 3 else default_lang)


@app.command()
def mux(
    paths: Annotated[list[Path], typer.Argument(help="One or more audio files, directories, or glob patterns.")],
    force: Annotated[bool, typer.Option("-f", "--force", help="Overwrite the target .mkv if it exists.")] = False,
    keep_original: Annotated[bool, typer.Option("--keep-original", help="Keep the source audio next to the new .mkv instead of replacing it.")] = False,
    language: Annotated[str, typer.Option("--language", help="Override the language tag on the audio + subtitle streams (e.g. heb, eng). Default: use the detected language from the asr row, else the config default.")] = "",
    no_srt: Annotated[bool, typer.Option("--no-srt", help="Don't embed the .srt sidecar even if present.")] = False,
    recursive: Annotated[bool, typer.Option("-r", "--recursive", help="When a source is a directory, walk its whole subtree.")] = False,
    insensitive: Annotated[bool, typer.Option("-i", "--insensitive", help="Case-insensitive glob matching.")] = False,
    stop_on_error: Annotated[bool, typer.Option("--stop-on-error", help="Stop at the first failing file.")] = False,
) -> None:
    """Remux audio files into MKV with the .srt sidecar attached.

    The container holds three streams: a tiny dummy black H.264 video
    (so MPC-HC's subtitle UI activates), Opus-encoded audio (VOIP preset
    for narrowband phone calls, generic audio preset for wider sources),
    and the SubRip subtitle stream tagged with the audio's language. The
    .mkv lands next to the source with the source's mtime/ctime
    restored.
    """
    from dragndoc import asr_artifacts
    from dragndoc.cli._path_args import expand_paths
    from dragndoc.config import get_settings
    from dragndoc.db import transaction
    from dragndoc.metadata.hashing import hash_file
    from dragndoc.meta_store import get_by_file, relative_to_root
    from dragndoc.mux import mkvmerge_available, mux_one

    log.info("CLI: mux %d arg(s) (force=%s keep_original=%s lang=%s no_srt=%s recursive=%s)",
             len(paths), force, keep_original, language or "(auto)", no_srt, recursive)

    if not mkvmerge_available():
        typer.echo(
            "mkvmerge is not installed. Install MKVToolNix:\n"
            "  winget install MoritzBunkus.MKVToolNix",
            err=True,
        )
        raise typer.Exit(2)

    expanded = expand_paths(paths, recursive=recursive, insensitive=insensitive)
    audio_paths = _filter_audio(expanded)
    if not audio_paths:
        typer.echo(f"No audio files matched: {', '.join(str(p) for p in paths)}", err=True)
        raise typer.Exit(1)

    settings = get_settings()
    failures = 0
    successes = 0
    total_src = 0
    total_dst = 0

    for src in audio_paths:
        doc = get_by_file(src)
        srt = None if no_srt else asr_artifacts.srt_sidecar_for(src)
        if srt and not srt.exists():
            srt = None
        lang = language.strip() or _resolve_language(doc, settings.mux.default_language)

        try:
            result = mux_one(
                src,
                srt=srt,
                language=lang,
                force=force,
                keep_original=keep_original,
            )
        except (FileExistsError, FileNotFoundError, RuntimeError) as exc:
            failures += 1
            typer.echo(f"FAILED {src}: {exc}", err=True)
            if stop_on_error:
                raise typer.Exit(1) from exc
            continue

        # update the docs row to point at the new file (path + hash + size)
        new_rel = relative_to_root(result.dst_mkv)
        if doc is not None:
            new_hash = hash_file(result.dst_mkv)
            new_size = result.dst_bytes
            old_rel = doc.path
            with transaction() as conn:
                if old_rel != new_rel:
                    conn.execute("UPDATE docs SET path = ? WHERE path = ?", (new_rel, old_rel))
                conn.execute("UPDATE docs SET hash = ?, size = ? WHERE path = ?", (new_hash, new_size, new_rel))

        successes += 1
        total_src += result.src_bytes
        total_dst += result.dst_bytes
        saved = result.src_bytes - result.dst_bytes
        saved_pct = (100.0 * saved / result.src_bytes) if result.src_bytes else 0.0
        msg = (
            f"MUXED  {src.name} -> {result.dst_mkv.name}  "
            f"[{result.preset.application} @ {result.preset.bitrate}, lang={result.language}, "
            f"{result.src_bytes:>9} -> {result.dst_bytes:>9} bytes, {saved_pct:+.0f}%]"
        )
        if not result.replaced_original:
            msg += "  (kept original)"
        typer.echo(msg)

    if successes:
        saved = total_src - total_dst
        saved_pct = (100.0 * saved / total_src) if total_src else 0.0
        typer.echo(
            f"\nMuxed {successes} file(s); failed {failures}. "
            f"Size: {total_src} -> {total_dst} bytes ({saved_pct:+.0f}%)."
        )
    if failures and not successes:
        raise typer.Exit(1)
