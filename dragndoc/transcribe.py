"""Faster-whisper wrapper + ffmpeg/ffprobe + channel-split + language detect.

Mirrors :mod:`dragndoc.ocr`: lazy engine init, a single module-level
``WhisperModel`` instance cached by ``(model, device, compute_type)``,
graceful degradation when the engine or its system prereqs are missing.

Returns a structured :class:`TranscriptionResult` (segments + plain text +
SRT view + detected language + channels) rather than just ``(text, lang)``.
The ``engine="simple"`` path uses faster-whisper unchanged; ``engine="diarized"``
opt-in path is wired in Phase 3 (whisperx + pyannote) and falls back to
``simple`` on any failure.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Iterable

from dragndoc.config import get_settings
from dragndoc.log import get_logger
from dragndoc.paths import asr_models_dir


log = get_logger(__name__)


_MODEL_CACHE: dict[tuple[str, str, str], Any] = {}
_DLL_DIRS_REGISTERED = False


# ---------------------------------------------------------------------------
# Windows: expose pip-installed nvidia cuBLAS/cuDNN to ctranslate2 / torch
# ---------------------------------------------------------------------------


def _register_cuda_dll_dirs() -> None:
    """On Windows, expose the pip-installed nvidia cuBLAS/cuDNN DLLs to ctranslate2.

    faster-whisper's CUDA backend needs ``cublas64_12.dll`` / ``cudnn64_*.dll``
    on the DLL search path. The ``nvidia-cublas-cu12`` and ``nvidia-cudnn-cu12``
    wheels drop them into ``site-packages/nvidia/{cublas,cudnn}/bin``. We add
    them via ``os.add_dll_directory`` and prepend to PATH because ctranslate2
    lazy-loads through ``LoadLibrary`` which honors PATH (not just
    dll-directories). Idempotent.
    """
    global _DLL_DIRS_REGISTERED
    if _DLL_DIRS_REGISTERED or sys.platform != "win32":
        _DLL_DIRS_REGISTERED = True
        return
    try:
        import nvidia  # type: ignore
    except ImportError:
        _DLL_DIRS_REGISTERED = True
        return
    roots: list[Path] = []
    for entry in getattr(nvidia, "__path__", []) or []:
        roots.append(Path(entry))
    bin_dirs: list[str] = []
    for site_root in roots:
        for sub in ("cublas", "cudnn", "cuda_runtime", "cuda_nvrtc"):
            bin_dir = site_root / sub / "bin"
            if bin_dir.is_dir():
                bin_dirs.append(str(bin_dir))
                try:
                    os.add_dll_directory(str(bin_dir))
                except (OSError, AttributeError):
                    pass
    if bin_dirs:
        sep = os.pathsep
        existing = os.environ.get("PATH", "")
        os.environ["PATH"] = sep.join([*bin_dirs, existing]) if existing else sep.join(bin_dirs)
    _DLL_DIRS_REGISTERED = True


_register_cuda_dll_dirs()


# ---------------------------------------------------------------------------
# data shapes
# ---------------------------------------------------------------------------


@dataclass
class TranscriptSegment:
    """One bounded slice of transcript with timing + optional speaker."""

    start: float                    # seconds since stream start
    end: float
    text: str
    speaker: str | None = None      # "CHANNEL_0" / "SPEAKER_00" / None
    language: str | None = None     # per-segment language when known


@dataclass
class TranscriptionResult:
    """Everything ``transcribe()`` / ``transcribe_channels()`` produces."""

    segments: list[TranscriptSegment] = field(default_factory=list)
    text: str = ""                  # joined plain text (LLM-facing, no labels)
    srt: str = ""                   # SRT view with speaker prefix when present
    language: str | None = None     # primary detected language
    language_probability: float | None = None
    duration_seconds: float | None = None
    channels: int = 1
    diarized: bool = False          # True only when pyannote labels are present
    engine: str = "faster-whisper"

    def speakers(self) -> list[str]:
        """Distinct speaker labels in order of first appearance."""
        seen: list[str] = []
        for seg in self.segments:
            if seg.speaker and seg.speaker not in seen:
                seen.append(seg.speaker)
        return seen

    def text_with_speakers(self) -> str:
        """Joined transcript with ``[speaker]`` prefixes when 2+ distinct speakers.

        Used as the LLM-facing body for diarized / channel-split recordings —
        speaker structure carries real meaning ("I said" vs "they said") that
        the bare-text join discards. Falls back to plain :attr:`text` for
        single-speaker (or speakerless) recordings to keep prompts compact.
        """
        speakers = self.speakers()
        if len(speakers) < 2:
            return self.text
        out: list[str] = []
        current_speaker: str | None = None
        buffer: list[str] = []
        for seg in self.segments:
            text = (seg.text or "").strip()
            if not text:
                continue
            if seg.speaker != current_speaker:
                if buffer:
                    out.append(" ".join(buffer))
                    buffer = []
                current_speaker = seg.speaker
                buffer.append(f"[{seg.speaker}] {text}" if seg.speaker else text)
            else:
                buffer.append(text)
        if buffer:
            out.append(" ".join(buffer))
        return "\n".join(out)


# ---------------------------------------------------------------------------
# system prereq checks
# ---------------------------------------------------------------------------


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def ffprobe_available() -> bool:
    return shutil.which("ffprobe") is not None


def whisper_available() -> bool:
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        return False
    return True


def whisperx_available() -> bool:
    try:
        import whisperx  # noqa: F401
    except ImportError:
        return False
    return True


def whisper_version() -> str:
    try:
        import faster_whisper

        return f"faster-whisper {faster_whisper.__version__}"
    except Exception:
        return "faster-whisper unknown"


def whisperx_version() -> str:
    try:
        import whisperx

        return f"whisperx {getattr(whisperx, '__version__', 'unknown')}"
    except Exception:
        return "whisperx unknown"


def engine_version(engine: str = "faster-whisper") -> str:
    """One-line engine identity string for the ``asr.engine_ver`` column."""
    settings = get_settings()
    if engine == "whisperx":
        return f"{whisperx_version()} model={settings.asr.model}"
    return f"{whisper_version()} model={settings.asr.model}"


def hf_token() -> str:
    """Resolve the Hugging Face token (settings → HF_TOKEN → HUGGINGFACE_TOKEN)."""
    settings = get_settings()
    return (
        (getattr(settings.asr, "hf_token", "") or "")
        or os.environ.get("HF_TOKEN", "")
        or os.environ.get("HUGGINGFACE_TOKEN", "")
    )


# ---------------------------------------------------------------------------
# model resolution
# ---------------------------------------------------------------------------


def _resolve_model_dir() -> Path | None:
    """Return the asr-models dir if it exists/can be created, else ``None``."""
    try:
        target = asr_models_dir()
    except Exception:  # noqa: BLE001
        return None
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return target


def _resolve_device(setting: str) -> str:
    if setting and setting != "auto":
        return setting
    try:
        import ctranslate2

        if ctranslate2.get_cuda_device_count() > 0:
            return "cuda"
    except Exception:  # noqa: BLE001
        pass
    return "cpu"


def _resolve_compute_type(device: str, setting: str) -> str:
    if setting and setting != "auto":
        return setting
    return "float16" if device == "cuda" else "int8"


def _load_model() -> Any:
    """Build (or reuse) the faster-whisper model for the current settings."""
    from faster_whisper import WhisperModel

    settings = get_settings()
    asr = settings.asr
    device = _resolve_device(asr.device)
    compute = _resolve_compute_type(device, asr.compute_type)
    key = (asr.model, device, compute)
    cached = _MODEL_CACHE.get(key)
    if cached is not None:
        return cached
    model_dir = _resolve_model_dir()
    download_root = str(model_dir) if model_dir is not None else None
    log.info(
        "Loading Whisper model %s on %s/%s (cache=%s)",
        asr.model, device, compute, download_root or "<default>",
    )
    try:
        model = WhisperModel(
            asr.model,
            device=device,
            compute_type=compute,
            download_root=download_root,
        )
    except Exception as exc:  # noqa: BLE001
        if device == "cuda":
            log.warning("CUDA Whisper init failed (%s); falling back to CPU int8.", exc)
            device = "cpu"
            compute = "int8"
            key = (asr.model, device, compute)
            model = WhisperModel(
                asr.model,
                device=device,
                compute_type=compute,
                download_root=download_root,
            )
        else:
            raise
    _MODEL_CACHE[key] = model
    return model


# ---------------------------------------------------------------------------
# srt formatter
# ---------------------------------------------------------------------------


def _srt_timestamp(seconds: float) -> str:
    """Format seconds as ``HH:MM:SS,mmm`` per the SRT spec."""
    if seconds < 0:
        seconds = 0.0
    total_ms = int(round(seconds * 1000))
    hours, rem = divmod(total_ms, 3600 * 1000)
    minutes, rem = divmod(rem, 60 * 1000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


import re as _re

_RTL_CHAR_RE = _re.compile(
    "["
    "֐-׿"   # Hebrew
    "؀-ۿ"   # Arabic
    "ݐ-ݿ"   # Arabic Supplement
    "ࢠ-ࣿ"   # Arabic Extended-A
    "יִ-﷿"   # Hebrew + Arabic Presentation Forms-A
    "ﹰ-﻿"   # Arabic Presentation Forms-B
    "]"
)

# Match maximal contiguous Latin-script runs (with neutrals only between
# Latin letters, never as leading/trailing chars — so the space adjacent
# to a Hebrew chunk isn't swept into the LTR wrapper).
_LTR_RUN_RE = _re.compile(
    r"""[A-Za-z](?:[A-Za-z0-9_ .,;:!?'"()\[\]\-+/]*[A-Za-z0-9_])?"""
)

# Unicode bidi controls. PDF (U+202C) pops whichever embedding was most
# recently pushed, so nested RLE…(LRE…PDF)…PDF composes correctly.
_RLE = "‫"   # right-to-left embedding (outer wrap for RTL paragraph)
_LRE = "‪"   # left-to-right embedding (inner wrap for Latin run)
_PDF = "‬"   # pop directional formatting


def _wrap_line_for_bidi(line: str) -> str:
    """Wrap mixed RTL/LTR lines with Unicode directional formatting.

    Two layers — same philosophy as the hebrew-office skill's per-paragraph
    + per-run RTL/LTR markup, just translated to SRT's text-only world:

    1. Each contiguous Latin run inside the line is wrapped with
       ``LRE…PDF`` so multi-word LTR sequences ("Bob Smith", "INC.") stay
       together as one LTR scope and don't fragment on internal spaces
       under weak bidi renderers.
    2. The whole line is then wrapped with ``RLE…PDF`` so the paragraph
       direction is unambiguously RTL.

    Pure LTR lines pass through unchanged. Logical character order is
    preserved — these are direction *hints*, not character reordering.
    """
    if not _RTL_CHAR_RE.search(line):
        return line
    inner = _LTR_RUN_RE.sub(lambda m: f"{_LRE}{m.group(0)}{_PDF}", line)
    return f"{_RLE}{inner}{_PDF}"


def to_srt(segments: Iterable[TranscriptSegment]) -> str:
    """Render segments as SRT.

    When a segment has a ``speaker`` label, prefix the text with
    ``[speaker] ``; otherwise emit just the text. Empty segments are
    skipped. Lines containing RTL scripts (Hebrew, Arabic) get
    standards-compliant directional formatting (see
    :func:`_wrap_line_for_bidi`).
    """
    lines: list[str] = []
    index = 0
    for seg in segments:
        text = (seg.text or "").strip()
        if not text:
            continue
        index += 1
        prefix = f"[{seg.speaker}] " if seg.speaker else ""
        cue_text = f"{prefix}{text}"
        # subtitle cues can be multi-line; wrap each visual line independently
        cue_lines = cue_text.splitlines() or [cue_text]
        wrapped_lines = [_wrap_line_for_bidi(ln) for ln in cue_lines]
        lines.append(str(index))
        lines.append(f"{_srt_timestamp(seg.start)} --> {_srt_timestamp(seg.end)}")
        lines.extend(wrapped_lines)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n" if lines else ""


# ---------------------------------------------------------------------------
# language detection
# ---------------------------------------------------------------------------


def _hint_language(langs: str | None = None) -> str | None:
    """If the configured langs collapse to a single value, return it; else None."""
    settings = get_settings()
    raw = langs if langs is not None else settings.asr.langs
    cleaned = [s.strip() for s in (raw or "").replace("+", ",").split(",") if s.strip()]
    return cleaned[0] if len(cleaned) == 1 else None


def detect_language(
    audio: Path | str | BinaryIO,
    *,
    sample_seconds: int | None = None,
) -> tuple[str | None, float]:
    """Detect the dominant language in the first ``sample_seconds`` of audio.

    Returns ``(language_code, probability)``. ``language_code`` is ``None``
    when detection fails. Cheap — no full transcription happens.

    Implementation: faster-whisper's ``detect_language`` API consumes a
    numpy float32 buffer at 16 kHz mono. We use the model's own
    ``decode_audio`` helper if available, else fall back to ffmpeg piping.
    """
    if not whisper_available():
        raise RuntimeError("faster-whisper is not installed.")
    settings = get_settings()
    seconds = int(sample_seconds or settings.asr.language_detection_seconds or 30)
    model = _load_model()

    try:
        audio_array = _decode_audio_for_detect(audio, seconds=seconds)
    except Exception as exc:  # noqa: BLE001
        log.warning("Language detect: audio decode failed (%s); skipping.", exc)
        return None, 0.0

    if audio_array is None or len(audio_array) == 0:
        return None, 0.0

    try:
        lang, prob, _all_probs = model.detect_language(audio_array)
    except Exception as exc:  # noqa: BLE001
        log.warning("Language detect failed (%s); returning None.", exc)
        return None, 0.0
    log.info("Language detected: %s (prob=%.3f)", lang, prob)
    return lang, float(prob)


def _decode_audio_for_detect(
    audio: Path | str | BinaryIO,
    *,
    seconds: int,
) -> Any:
    """Return a 16 kHz mono float32 numpy array for the first ``seconds``."""
    try:
        from faster_whisper.audio import decode_audio
    except ImportError as exc:
        raise RuntimeError("faster_whisper.audio.decode_audio is required") from exc
    if isinstance(audio, (str, Path)):
        return decode_audio(str(audio), sampling_rate=16000)[: 16000 * seconds]
    # buffer
    return decode_audio(audio, sampling_rate=16000)[: 16000 * seconds]


# ---------------------------------------------------------------------------
# core transcribe()
# ---------------------------------------------------------------------------


def transcribe(
    audio: Path | str | BinaryIO,
    *,
    language: str | None = None,
    langs: str | None = None,
    engine: str = "simple",
    speaker_label: str | None = None,
) -> TranscriptionResult:
    """Transcribe an audio source.

    ``engine``:
        - ``"simple"`` (default): faster-whisper.
        - ``"diarized"``: WhisperX + pyannote (Phase 3). Falls back to
          ``"simple"`` if whisperx or pyannote aren't installed / the
          HF token is missing / model load fails.

    ``language``: explicit ISO 639-1/2 code (e.g. ``"he"``). When omitted,
    falls back to the configured ``asr.langs`` if it's a single value,
    else lets Whisper auto-detect.

    ``speaker_label``: when supplied, every emitted segment is tagged with
    this speaker name (used by :func:`transcribe_channels` to label each
    channel's output before merging).
    """
    if not whisper_available():
        raise RuntimeError("faster-whisper is not installed.")

    if engine == "diarized":
        try:
            return _transcribe_diarized(audio, language=language, langs=langs)
        except Exception as exc:  # noqa: BLE001
            log.warning("Diarized engine failed (%s); falling back to simple.", exc)
            engine = "simple"

    settings = get_settings()
    asr = settings.asr
    model = _load_model()

    if language is None:
        language = _hint_language(langs)

    source: Any
    label: str
    if isinstance(audio, (str, Path)):
        source = str(audio)
        label = Path(source).name
    else:
        source = audio
        label = "<buffer>"
    log.info("ASR starting (%s): %s (language=%s)", engine, label, language or "auto")

    import time as _time
    started = _time.perf_counter()
    segments_iter, info = model.transcribe(
        source,
        language=language,
        beam_size=asr.beam_size,
        vad_filter=asr.vad_filter,
    )
    segments: list[TranscriptSegment] = []
    for seg in segments_iter:
        text = (seg.text or "").strip()
        if not text:
            continue
        segments.append(TranscriptSegment(
            start=float(getattr(seg, "start", 0.0) or 0.0),
            end=float(getattr(seg, "end", 0.0) or 0.0),
            text=text,
            speaker=speaker_label,
        ))
    text_joined = " ".join(s.text for s in segments).strip()
    duration = float(getattr(info, "duration", 0.0) or 0.0)
    detected_lang = getattr(info, "language", None)
    detected_prob = float(getattr(info, "language_probability", 0.0) or 0.0)
    log.info(
        "ASR done: %d segs, %d chars (lang=%s prob=%.2f) in %dms",
        len(segments), len(text_joined), detected_lang, detected_prob,
        int((_time.perf_counter() - started) * 1000),
    )
    return TranscriptionResult(
        segments=segments,
        text=text_joined,
        srt=to_srt(segments),
        language=detected_lang,
        language_probability=detected_prob,
        duration_seconds=duration or None,
        channels=1,
        diarized=False,
        engine="faster-whisper",
    )


def transcribe_bytes(
    audio_bytes: bytes,
    *,
    language: str | None = None,
    langs: str | None = None,
    engine: str = "simple",
) -> TranscriptionResult:
    """Convenience wrapper around :func:`transcribe` for raw audio bytes."""
    return transcribe(io.BytesIO(audio_bytes), language=language, langs=langs, engine=engine)


# ---------------------------------------------------------------------------
# channel-split (Phase 2b)
# ---------------------------------------------------------------------------


def transcribe_channels(
    audio: Path,
    *,
    language: str | None = None,
    channel_count: int | None = None,
    labels: list[str] | None = None,
) -> TranscriptionResult:
    """Split a multi-channel recording into per-channel WAVs and transcribe each.

    Returns one merged :class:`TranscriptionResult` with segments labeled
    ``labels[i]`` (default ``CHANNEL_0`` / ``CHANNEL_1`` / …) and ordered
    by ``start`` time. Channel splitting is ground-truth speaker separation,
    so the result is marked ``diarized=False`` (we reserve that flag for
    pyannote-derived diarization).
    """
    if not ffmpeg_available():
        raise RuntimeError("ffmpeg is not installed or on PATH.")
    if channel_count is None:
        streams = ffprobe_streams(audio)
        audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
        if not audio_streams:
            raise RuntimeError(f"No audio stream in {audio}")
        channel_count = int(audio_streams[0].get("channels") or 1)
    if channel_count < 2:
        # caller's job is to check first, but be defensive
        result = transcribe(audio, language=language)
        result.channels = channel_count
        return result

    if labels is None:
        labels = [f"CHANNEL_{i}" for i in range(channel_count)]
    if len(labels) < channel_count:
        labels = list(labels) + [f"CHANNEL_{i}" for i in range(len(labels), channel_count)]

    all_segments: list[TranscriptSegment] = []
    detected_lang: str | None = None
    detected_prob: float | None = None
    duration: float | None = None

    with tempfile.TemporaryDirectory(prefix="dnd_asr_chan_") as td:
        td_path = Path(td)
        for ch in range(channel_count):
            wav_path = td_path / f"ch{ch}.wav"
            _ffmpeg_extract_channel(audio, ch, wav_path)
            part = transcribe(wav_path, language=language, speaker_label=labels[ch])
            all_segments.extend(part.segments)
            if detected_lang is None:
                detected_lang = part.language
                detected_prob = part.language_probability
            if duration is None or (part.duration_seconds and part.duration_seconds > (duration or 0.0)):
                duration = part.duration_seconds

    all_segments.sort(key=lambda s: (s.start, s.end))
    text_joined = " ".join(s.text for s in all_segments).strip()
    return TranscriptionResult(
        segments=all_segments,
        text=text_joined,
        srt=to_srt(all_segments),
        language=detected_lang,
        language_probability=detected_prob,
        duration_seconds=duration,
        channels=channel_count,
        diarized=False,
        engine="faster-whisper",
    )


def _ffmpeg_extract_channel(source: Path, channel_index: int, dest: Path) -> None:
    """Pull one channel out of ``source`` into a 16 kHz mono WAV at ``dest``."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is not installed or on PATH.")
    cmd = [
        ffmpeg, "-v", "error", "-y",
        "-i", str(source),
        "-map_channel", f"0.0.{channel_index}",
        "-ac", "1",
        "-ar", "16000",
        "-f", "wav",
        str(dest),
    ]
    proc = subprocess.run(cmd, capture_output=True, check=False)
    if proc.returncode != 0:
        msg = proc.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(f"ffmpeg channel-extract failed for channel {channel_index}: {msg}")


# ---------------------------------------------------------------------------
# diarized engine (Phase 3 — opt-in, lazy)
# ---------------------------------------------------------------------------


def _transcribe_diarized(
    audio: Path | str | BinaryIO,
    *,
    language: str | None = None,
    langs: str | None = None,
) -> TranscriptionResult:
    """WhisperX + alignment + pyannote. Imported lazily; raises if missing."""
    import whisperx  # type: ignore

    if not isinstance(audio, (str, Path)):
        # whisperx wants a path; spill the buffer to a temp wav
        raise NotImplementedError("diarized engine currently requires a path, not a buffer")

    settings = get_settings()
    asr = settings.asr
    device = _resolve_device(asr.device)
    compute = _resolve_compute_type(device, asr.compute_type)
    model_dir = _resolve_model_dir()
    download_root = str(model_dir) if model_dir is not None else None

    log.info("WhisperX starting on %s/%s for %s", device, compute, Path(str(audio)).name)
    wx_model = whisperx.load_model(
        asr.model,
        device=device,
        compute_type=compute,
        download_root=download_root,
    )
    audio_array = whisperx.load_audio(str(audio))
    transcription = wx_model.transcribe(
        audio_array,
        language=language or _hint_language(langs),
        batch_size=getattr(asr, "batch_size", 16),
    )
    detected_lang = transcription.get("language")

    # alignment (best-effort)
    try:
        align_model_id = (asr.alignment_models or {}).get(detected_lang or "")
        if align_model_id:
            align_model, metadata = whisperx.load_align_model(
                language_code=detected_lang,
                device=device,
                model_name=align_model_id,
            )
            transcription = whisperx.align(
                transcription["segments"], align_model, metadata, audio_array, device,
                return_char_alignments=False,
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("WhisperX alignment failed (%s); using segment-level timings.", exc)

    # diarization (best-effort, requires HF token)
    diarized = False
    token = hf_token()
    if token:
        try:
            from whisperx import DiarizationPipeline  # type: ignore

            diarize_model = DiarizationPipeline(use_auth_token=token, device=device)
            diarize_segments = diarize_model(audio_array)
            transcription = whisperx.assign_word_speakers(diarize_segments, transcription)
            diarized = True
        except Exception as exc:  # noqa: BLE001
            log.warning("WhisperX diarization failed (%s); transcript without speakers.", exc)
    else:
        log.info("HF token unset — skipping diarization.")

    segments: list[TranscriptSegment] = []
    for seg in transcription.get("segments", []):
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        segments.append(TranscriptSegment(
            start=float(seg.get("start") or 0.0),
            end=float(seg.get("end") or 0.0),
            text=text,
            speaker=seg.get("speaker") or None,
        ))
    text_joined = " ".join(s.text for s in segments).strip()
    return TranscriptionResult(
        segments=segments,
        text=text_joined,
        srt=to_srt(segments),
        language=detected_lang,
        language_probability=None,
        duration_seconds=None,
        channels=1,
        diarized=diarized,
        engine="whisperx",
    )


# ---------------------------------------------------------------------------
# ffprobe helpers (used by the video extractor + classifier)
# ---------------------------------------------------------------------------


def ffprobe_streams(path: Path) -> list[dict[str, Any]]:
    """Return ffprobe's per-stream metadata as a list of plain dicts."""
    if not ffprobe_available():
        raise RuntimeError("ffprobe is not installed or on PATH.")
    proc = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-show_streams",
            "-show_format",
            "-print_format", "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {path}: {proc.stderr.strip()}")
    data = json.loads(proc.stdout or "{}")
    streams = data.get("streams") or []
    if isinstance(streams, list):
        return [s for s in streams if isinstance(s, dict)]
    return []


def ffprobe_format(path: Path) -> dict[str, Any]:
    """Return ffprobe's container-level metadata block."""
    if not ffprobe_available():
        raise RuntimeError("ffprobe is not installed or on PATH.")
    proc = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-show_format",
            "-print_format", "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        return {}
    data = json.loads(proc.stdout or "{}")
    fmt = data.get("format")
    return fmt if isinstance(fmt, dict) else {}


def audio_channels(path: Path) -> int:
    """Return the channel count of the first audio stream (1 if unreadable)."""
    try:
        streams = ffprobe_streams(path)
    except Exception:  # noqa: BLE001
        return 1
    for s in streams:
        if s.get("codec_type") == "audio":
            try:
                return int(s.get("channels") or 1)
            except (TypeError, ValueError):
                return 1
    return 1
