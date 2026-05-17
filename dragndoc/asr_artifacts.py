"""SRT sidecar + JSON twin persistence for transcripts.

The SRT lives next to the original audio/video (`<base>.srt`); the JSON
twin lives under `data/transcripts/<doc-id>.json` and is the canonical
source for regenerating the SRT after a speaker rename. Other modules
treat the SRT as a *view* of the JSON.

`sidecar_paths_for(original)` is the central helper used by file ops
(`dnd mv` / `cp` / `rm`) and the scanner to know which related files
follow the parent file.

The hand-edit guard: if `<base>.srt` exists with mtime ≥ the JSON
twin's mtime, treat the SRT as user-edited and refuse to overwrite
unless ``force=True`` is passed.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import asdict
from pathlib import Path

from dragndoc.config import REPO_ROOT, get_settings
from dragndoc.log import get_logger
from dragndoc.transcribe import TranscriptionResult, TranscriptSegment, to_srt


log = get_logger(__name__)


SRT_SUFFIX = ".srt"
KNOWN_SIDECAR_SUFFIXES = (SRT_SUFFIX,)


# ---------------------------------------------------------------------------
# path resolution
# ---------------------------------------------------------------------------


def transcripts_dir() -> Path:
    """Resolve ``asr.transcripts_dir`` relative to the repo root."""
    raw = get_settings().asr.transcripts_dir or "data/transcripts"
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = REPO_ROOT / p
    return p.resolve()


def srt_sidecar_for(original: Path) -> Path:
    """The ``<original>.srt`` path sibling to ``original`` (does not check exist)."""
    return original.with_suffix(original.suffix + SRT_SUFFIX) if False else original.with_suffix(SRT_SUFFIX)


def json_path_for(doc_id: int) -> Path:
    return transcripts_dir() / f"{doc_id}.json"


def sidecar_paths_for(original: Path) -> list[Path]:
    """Return *existing* sidecar files next to ``original``.

    Today only `.srt`; extensible later. Filename match is exact: same
    stem, recognized suffix. Case-sensitive on POSIX, case-insensitive
    on Windows (relying on the filesystem).
    """
    out: list[Path] = []
    for suffix in KNOWN_SIDECAR_SUFFIXES:
        candidate = original.with_suffix(suffix)
        if candidate.exists() and candidate.is_file():
            out.append(candidate)
    return out


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------


def _serialize_result(result: TranscriptionResult) -> dict:
    """JSON-serializable representation of a TranscriptionResult."""
    return {
        "language": result.language,
        "language_probability": result.language_probability,
        "duration_seconds": result.duration_seconds,
        "channels": result.channels,
        "diarized": result.diarized,
        "engine": result.engine,
        "segments": [
            {
                "start": s.start,
                "end": s.end,
                "text": s.text,
                "speaker": s.speaker,
                "language": s.language,
            }
            for s in result.segments
        ],
        "text": result.text,
    }


def _deserialize_result(payload: dict) -> TranscriptionResult:
    segments = [
        TranscriptSegment(
            start=float(seg.get("start") or 0.0),
            end=float(seg.get("end") or 0.0),
            text=str(seg.get("text") or ""),
            speaker=seg.get("speaker"),
            language=seg.get("language"),
        )
        for seg in payload.get("segments", [])
    ]
    return TranscriptionResult(
        segments=segments,
        text=str(payload.get("text") or ""),
        srt=to_srt(segments),
        language=payload.get("language"),
        language_probability=payload.get("language_probability"),
        duration_seconds=payload.get("duration_seconds"),
        channels=int(payload.get("channels") or 1),
        diarized=bool(payload.get("diarized")),
        engine=str(payload.get("engine") or "faster-whisper"),
    )


def _write_text(path: Path, text: str, *, with_bom: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = text
    if with_bom:
        data = ("﻿" if not data.startswith("﻿") else "") + data
    path.write_text(data, encoding="utf-8")


def _srt_user_edited(srt_path: Path, json_path: Path) -> bool:
    """True if the SRT looks like it was edited after the JSON twin was written."""
    try:
        srt_mtime = srt_path.stat().st_mtime
    except OSError:
        return False
    try:
        json_mtime = json_path.stat().st_mtime
    except OSError:
        # no JSON twin → conservatively treat as user-edited
        return True
    # small slack for filesystem mtime granularity
    return srt_mtime > json_mtime + 1.0


def save(
    result: TranscriptionResult,
    *,
    original: Path,
    doc_id: int,
    force: bool = False,
) -> tuple[Path | None, Path | None]:
    """Persist the SRT sidecar (next to ``original``) and the JSON twin.

    Returns ``(srt_path, json_path)``; either may be ``None`` when the
    write is skipped by config or the hand-edit guard. The JSON twin is
    *always* written when ``asr.save_json=true`` (it's our canonical
    source); the SRT respects the hand-edit guard.
    """
    settings = get_settings()
    asr = settings.asr
    srt_out: Path | None = None
    json_out: Path | None = None

    # JSON twin first — canonical state
    if asr.save_json:
        json_out = json_path_for(doc_id)
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(
            json.dumps(_serialize_result(result), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # SRT sidecar
    if asr.save_srt and result.srt:
        srt_target = srt_sidecar_for(original)
        if not force and srt_target.exists() and json_out and _srt_user_edited(srt_target, json_out):
            log.warning(
                "SRT appears hand-edited (newer than JSON twin); skipping overwrite: %s",
                srt_target,
            )
        else:
            try:
                _write_text(srt_target, result.srt, with_bom=asr.srt_utf8_bom)
                srt_out = srt_target
            except OSError as exc:
                log.warning("SRT write failed for %s: %s", srt_target, exc)
    return srt_out, json_out


def load_json(doc_id: int) -> TranscriptionResult | None:
    p = json_path_for(doc_id)
    if not p.exists():
        return None
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return _deserialize_result(payload)


# ---------------------------------------------------------------------------
# helpers used by file ops + scanner
# ---------------------------------------------------------------------------


def follow_move(src: Path, target: Path) -> list[tuple[Path, Path]]:
    """Move existing sidecars from ``src`` to ``target`` keeping basename in sync.

    Returns the list of ``(old, new)`` paths actually moved. Skips
    sidecars whose target already exists (refuses to overwrite).
    """
    moved: list[tuple[Path, Path]] = []
    for sidecar in sidecar_paths_for(src):
        suffix = sidecar.suffix
        # target stem follows the new audio's stem (e.g. renamed file)
        new_path = target.with_suffix(suffix)
        if new_path.exists():
            log.warning("Sidecar target exists; not moving: %s", new_path)
            continue
        try:
            shutil.move(str(sidecar), str(new_path))
        except OSError as exc:
            log.warning("Sidecar move failed (%s -> %s): %s", sidecar, new_path, exc)
            continue
        moved.append((sidecar, new_path))
    return moved


def follow_copy(src: Path, target: Path) -> list[tuple[Path, Path]]:
    """Copy existing sidecars from ``src`` to ``target``. Returns moved pairs."""
    copied: list[tuple[Path, Path]] = []
    for sidecar in sidecar_paths_for(src):
        new_path = target.with_suffix(sidecar.suffix)
        if new_path.exists():
            log.warning("Sidecar target exists; not copying: %s", new_path)
            continue
        try:
            shutil.copy2(str(sidecar), str(new_path))
        except OSError as exc:
            log.warning("Sidecar copy failed (%s -> %s): %s", sidecar, new_path, exc)
            continue
        copied.append((sidecar, new_path))
    return copied


def follow_rm(src: Path, *, purge: bool = False) -> list[Path]:
    """Delete existing sidecars next to ``src``. Returns deleted paths.

    By default sidecars go to the OS recycle bin; pass ``purge=True`` to
    bypass it.
    """
    from send2trash import send2trash

    deleted: list[Path] = []
    for sidecar in sidecar_paths_for(src):
        try:
            if purge:
                sidecar.unlink()
            else:
                send2trash(str(sidecar))
            deleted.append(sidecar)
        except OSError as exc:
            log.warning("Sidecar delete failed (%s): %s", sidecar, exc)
    return deleted


def delete_json_twin(doc_id: int, *, purge: bool = False) -> bool:
    """Best-effort delete of the JSON twin. Returns True if deleted.

    By default the file goes to the OS recycle bin; pass ``purge=True`` to
    bypass it.
    """
    from send2trash import send2trash

    p = json_path_for(doc_id)
    if not p.exists():
        return False
    try:
        if purge:
            p.unlink()
        else:
            send2trash(str(p))
        return True
    except OSError as exc:
        log.warning("JSON twin delete failed for doc_id=%d: %s", doc_id, exc)
        return False
