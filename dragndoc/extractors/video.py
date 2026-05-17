"""Video extractor: prefer embedded text subtitles; fall back to Whisper ASR.

Flow per file:

1. Probe streams with ``ffprobe``.
2. If a text-based subtitle track matches the configured ``asr.langs`` (and
   is not flagged forced/SDH), extract it with ``ffmpeg -map 0:s:N -f srt -``
   and treat the subtitle text as the transcript (engine="subtitle").
3. Otherwise (image-based subs, no subs, or no matching language), pipe the
   audio with ``ffmpeg -map 0:a:0 -ac 1 -ar 16000 -f wav -`` and feed it to
   :func:`dragndoc.transcribe.transcribe`.

Image-based subtitle codecs (``hdmv_pgs_subtitle``, ``dvd_subtitle``) are
skipped — they would need OCR, which is out of scope for this iteration.
"""

from __future__ import annotations

import io
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from dragndoc.config import get_settings
from dragndoc.extractors._asr import AsrTracker
from dragndoc.extractors._meta import collect
from dragndoc.extractors.base import CorruptDocumentError, ExtractedDoc, Section
from dragndoc.log import get_logger
from dragndoc.meta_store import AsrInfo
from dragndoc.transcribe import (
    TranscriptSegment,
    TranscriptionResult,
    ffmpeg_available,
    ffprobe_available,
    ffprobe_format,
    ffprobe_streams,
    to_srt,
    transcribe_bytes,
    whisper_available,
)


log = get_logger(__name__)


_IMAGE_SUB_CODECS = {
    "hdmv_pgs_subtitle",
    "dvd_subtitle",
    "dvb_subtitle",
    "xsub",
    "dvb_teletext",
}


def _ffmpeg_version() -> str:
    """One-line ffmpeg version string for the asr.engine_ver column."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return "ffmpeg unknown"
    try:
        proc = subprocess.run(
            [ffmpeg, "-version"],
            capture_output=True, text=True, check=False,
            encoding="utf-8", errors="replace",
        )
        first = (proc.stdout or "").splitlines()[0] if proc.stdout else ""
        return first.strip() or "ffmpeg"
    except Exception:  # noqa: BLE001
        return "ffmpeg"


def _normalize_lang_set(langs_csv: str) -> set[str]:
    """Parse ``"he,en"`` or ``"heb+eng"`` into a set of normalized lang codes.

    ffprobe reports 3-letter codes (``heb``, ``eng``); the config typically uses
    2-letter codes (``he``, ``en``). Accept both and compare loosely.
    """
    out: set[str] = set()
    for raw in re.split(r"[,+;]", langs_csv or ""):
        chunk = raw.strip().lower()
        if not chunk:
            continue
        out.add(chunk)
        out.add(_TWO_TO_THREE.get(chunk, chunk))
        out.add(_THREE_TO_TWO.get(chunk, chunk))
    return out


_TWO_TO_THREE = {"he": "heb", "en": "eng", "ar": "ara", "ru": "rus", "fr": "fra", "de": "deu", "es": "spa", "it": "ita", "ja": "jpn", "zh": "zho"}
_THREE_TO_TWO = {v: k for k, v in _TWO_TO_THREE.items()}


def _stream_tags(stream: dict[str, Any]) -> dict[str, str]:
    tags = stream.get("tags") or {}
    if not isinstance(tags, dict):
        return {}
    return {str(k).lower(): str(v) for k, v in tags.items() if isinstance(v, (str, int, float))}


def _stream_disposition(stream: dict[str, Any]) -> dict[str, int]:
    disp = stream.get("disposition") or {}
    if not isinstance(disp, dict):
        return {}
    return {str(k): int(v or 0) for k, v in disp.items() if isinstance(v, (int, float))}


def _select_subtitle_stream(
    streams: list[dict[str, Any]],
    wanted_langs: set[str],
) -> dict[str, Any] | None:
    """Return the best text-subtitle stream, or None if none qualify.

    Rules (in order):
    1. Skip non-subtitle and image-codec streams.
    2. Prefer streams whose tags.language matches ``wanted_langs``.
    3. Skip forced and SDH/hearing-impaired streams unless they're the only
       remaining option.
    4. When several remain, prefer the longest (proxy for main vs. signs).
    """
    subs = [s for s in streams if s.get("codec_type") == "subtitle"]
    if not subs:
        return None

    text_subs: list[dict[str, Any]] = []
    for s in subs:
        codec = (s.get("codec_name") or "").lower()
        if not codec or codec in _IMAGE_SUB_CODECS:
            continue
        text_subs.append(s)
    if not text_subs:
        return None

    matching = []
    for s in text_subs:
        lang = _stream_tags(s).get("language", "").lower()
        if not wanted_langs or lang in wanted_langs or not lang:
            matching.append(s)
    candidates = matching or text_subs

    main = [
        s for s in candidates
        if not _stream_disposition(s).get("forced")
        and not _stream_disposition(s).get("hearing_impaired")
    ] or candidates

    def _bias(stream: dict[str, Any]) -> tuple[int, int]:
        # prefer longer streams (more spoken content); fall back to index order
        try:
            tags = _stream_tags(stream)
            length = int(float(tags.get("number_of_frames") or 0))
        except (TypeError, ValueError):
            length = 0
        return (length, -int(stream.get("index") or 0))

    return max(main, key=_bias)


def _strip_srt(srt: str) -> str:
    """Strip SRT timing/index lines, leaving only spoken text."""
    if not srt:
        return ""
    out: list[str] = []
    for raw in srt.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.isdigit():
            continue
        if "-->" in line:
            continue
        # drop common html-ish formatting tags
        line = re.sub(r"<[^>]+>", "", line)
        line = re.sub(r"\{[^}]+\}", "", line)
        if line:
            out.append(line)
    return " ".join(out).strip()


def _extract_subtitle(path: Path, stream_index: int) -> str:
    """Run ``ffmpeg`` to convert the chosen subtitle stream to SRT text."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is not installed or on PATH.")
    proc = subprocess.run(
        [
            ffmpeg,
            "-v", "error",
            "-i", str(path),
            "-map", f"0:{stream_index}",
            "-f", "srt",
            "-",
        ],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg subtitle extract failed: {proc.stderr.decode('utf-8', 'replace').strip()}")
    return proc.stdout.decode("utf-8", "replace")


def _extract_audio_pcm(path: Path) -> bytes:
    """Pipe a mono 16 kHz wav PCM blob out of ``path`` for Whisper to consume."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is not installed or on PATH.")
    proc = subprocess.run(
        [
            ffmpeg,
            "-v", "error",
            "-i", str(path),
            "-map", "0:a:0",
            "-ac", "1",
            "-ar", "16000",
            "-f", "wav",
            "-",
        ],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg audio extract failed: {proc.stderr.decode('utf-8', 'replace').strip()}")
    return proc.stdout


def _container_metadata(path: Path) -> tuple[dict[str, Any], float | None]:
    """Pull container-level facts (duration, bitrate, format_name) via ffprobe."""
    try:
        fmt = ffprobe_format(path)
    except Exception:  # noqa: BLE001
        return {}, None
    flat: dict[str, Any] = {}
    duration: float | None = None
    for key in ("format_name", "format_long_name", "bit_rate", "size", "nb_streams"):
        value = fmt.get(key)
        if value not in (None, "", 0):
            flat[key] = value
    raw_dur = fmt.get("duration")
    try:
        if raw_dur is not None:
            duration = float(raw_dur)
            flat["duration"] = duration
    except (TypeError, ValueError):
        pass
    tags = fmt.get("tags") or {}
    if isinstance(tags, dict):
        for k, v in tags.items():
            if isinstance(v, (str, int, float)):
                flat[str(k)] = v
    return flat, duration


def extract(path: Path) -> ExtractedDoc:
    if not ffprobe_available():
        raise CorruptDocumentError("ffprobe is not installed or on PATH.")

    settings = get_settings()
    tracker = AsrTracker()
    metadata_raw, duration_seconds = _container_metadata(path)
    metadata = collect(metadata_raw, prefix="video_")

    try:
        streams = ffprobe_streams(path)
    except Exception as exc:  # noqa: BLE001
        raise CorruptDocumentError(f"ffprobe failed for {path}: {exc}") from exc

    wanted = _normalize_lang_set(settings.asr.langs)
    selected_sub = _select_subtitle_stream(streams, wanted)

    transcript = ""
    detected_lang: str | None = None
    duration_ms: int | None = None
    asr_info: AsrInfo | None = None
    decision = "no_asr"
    used_subs = False
    used_asr = False
    result: TranscriptionResult | None = None

    if selected_sub is not None:
        # subtitle path — extract, strip srt timing, treat as transcript
        srt_raw = ""
        try:
            srt_raw = _extract_subtitle(path, int(selected_sub["index"]))
            transcript = _strip_srt(srt_raw)
        except Exception as exc:  # noqa: BLE001
            log.warning("Subtitle extract failed for %s, falling back to ASR: %s", path.name, exc)
            transcript = ""

        if transcript:
            used_subs = True
            tracker.used_subtitle = True
            sub_lang = _stream_tags(selected_sub).get("language") or None
            detected_lang = _THREE_TO_TWO.get(sub_lang or "", sub_lang) if sub_lang else None
            decision = "asr_subtitle"
            asr_info = AsrInfo.for_subtitle_extract(
                langs=[detected_lang] if detected_lang else [],
                detected_lang=detected_lang,
                audio_seconds=duration_seconds,
                ffmpeg_ver=_ffmpeg_version(),
            )
            # synthesize a TranscriptionResult so the pipeline can write a
            # sidecar SRT from the embedded subtitle exactly like ASR-derived
            # transcripts
            result = TranscriptionResult(
                segments=[TranscriptSegment(start=0.0, end=duration_seconds or 0.0, text=transcript)],
                text=transcript,
                srt=srt_raw or to_srt([TranscriptSegment(start=0.0, end=duration_seconds or 0.0, text=transcript)]),
                language=detected_lang,
                duration_seconds=duration_seconds,
                channels=1,
                diarized=False,
                engine="subtitle",
            )

    if not transcript:
        # asr path — needs ffmpeg + whisper both
        if not whisper_available() or not ffmpeg_available():
            tracker.unavailable = True
        else:
            started = time.perf_counter()
            try:
                pcm = _extract_audio_pcm(path)
                if pcm:
                    result = transcribe_bytes(pcm, langs=settings.asr.langs, engine=settings.asr.engine)
                    transcript = result.text if result else ""
                    detected_lang = result.language if result else None
                    tracker.ran = bool(transcript)
                    used_asr = bool(transcript)
                else:
                    tracker.failed = True
            except Exception as exc:  # noqa: BLE001
                log.error("Video ASR failed for %s: %s", path, exc)
                tracker.failed = True
            duration_ms = int((time.perf_counter() - started) * 1000)
            if tracker.ran:
                decision = "asr_full"
                asr_info = AsrInfo.for_whisper_run(
                    decision,
                    detected_lang=detected_lang,
                    duration_ms=duration_ms,
                    audio_seconds=duration_seconds,
                )
                if result is not None:
                    asr_info.channels = result.channels
                    asr_info.diarized = result.diarized
                    asr_info.speakers = result.speakers()
                    asr_info.lang_prob = result.language_probability
        if not transcript and not used_subs:
            decision = tracker.decision(success="asr_full")

    if not transcript:
        if tracker.unavailable:
            metadata["asr_status"] = "unavailable"
        elif tracker.failed:
            metadata["asr_status"] = "failed"

    sections = [Section(label=None, text=transcript, index=0)]

    doc = ExtractedDoc(
        path=path,
        sections=sections,
        total_sections=None,
        format="video",
        ocr_used=False,
        ocr_decision="no_ocr",
        asr_used=used_asr or used_subs,
        asr_decision=decision,
        asr_info=asr_info,
        extracted_metadata=metadata,
    )
    doc.transcription = result  # type: ignore[attr-defined]
    return doc
