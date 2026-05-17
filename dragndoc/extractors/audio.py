"""Audio extractor: text comes from Whisper ASR; metadata from mutagen.

Mirrors :mod:`dragndoc.extractors.image`. Hands the path to
``transcribe.transcribe`` (or ``transcribe_channels`` when the file has
≥ 2 audio channels — phone-call recorders typically save stereo AMR
with one channel per party). Surfaces every populated mutagen tag plus
container-level facts (duration, bitrate, sample rate, channels, codec).
"""

from __future__ import annotations

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
    TranscriptionResult,
    audio_channels,
    detect_language,
    ffmpeg_available,
    transcribe,
    transcribe_channels,
    whisper_available,
)


log = get_logger(__name__)


def _read_container_metadata(path: Path) -> tuple[dict[str, Any], float | None]:
    """Extract mutagen tags + container facts. Returns (flat_dict, duration_seconds)."""
    out: dict[str, Any] = {}
    duration: float | None = None
    try:
        import mutagen

        mf = mutagen.File(str(path))
    except Exception as exc:  # noqa: BLE001
        log.debug("mutagen could not read %s: %s", path.name, exc)
        return out, None
    if mf is None:
        return out, None

    info = getattr(mf, "info", None)
    if info is not None:
        for attr in ("length", "bitrate", "sample_rate", "channels", "bits_per_sample", "codec"):
            value = getattr(info, attr, None)
            if value not in (None, "", 0):
                out[attr] = value
        if "length" in out:
            duration = float(out["length"])

    try:
        for key in mf.keys():
            value = mf.get(key)
            if value is None:
                continue
            if hasattr(value, "text"):
                value = list(value.text)
            elif isinstance(value, list) and len(value) == 1:
                value = value[0]
            out[str(key)] = value
    except Exception:  # noqa: BLE001
        pass

    return out, duration


def _pick_language(path: Path) -> tuple[str | None, float | None]:
    """Phase 1 — detect language on the first N seconds; gate by min_prob."""
    settings = get_settings()
    try:
        lang, prob = detect_language(path)
    except Exception as exc:  # noqa: BLE001
        log.warning("Language detection failed for %s: %s", path.name, exc)
        return None, None
    if lang and prob >= settings.asr.language_detection_min_prob:
        return lang, prob
    return None, prob


def extract(path: Path) -> ExtractedDoc:
    settings = get_settings()
    metadata_raw, duration_seconds = _read_container_metadata(path)
    metadata = collect(metadata_raw, prefix="audio_")

    # phase 4 — classify recording type from pre-transcription signals
    n_channels = audio_channels(path) if ffmpeg_available() else 1
    from dragndoc.recording_type import classify as classify_recording_type
    classification = classify_recording_type(
        path, audio_metadata=metadata, channels=n_channels,
    )
    rec_type = classification["recording_type"]
    log.info(
        "Recording-type for %s: %s (source=%s, committed=%s)",
        path.name, rec_type, classification["source"], classification["committed"],
    )

    tracker = AsrTracker()
    result: TranscriptionResult | None = None
    duration_ms: int | None = None
    detected_lang: str | None = None
    lang_prob: float | None = None
    skip_transcription = False

    # respect type-specific policy
    if rec_type == "non_speech":
        log.info("Skipping ASR for %s: classified as non_speech", path.name)
        skip_transcription = True
    elif rec_type == "music" and not settings.asr.transcribe_music:
        log.info("Skipping ASR for %s: music + transcribe_music=false", path.name)
        skip_transcription = True

    if skip_transcription:
        pass  # leave tracker default (no_asr)
    elif not whisper_available():
        log.warning("ASR requested for %s but faster-whisper is not installed.", path.name)
        tracker.unavailable = True
    elif not ffmpeg_available():
        log.warning("ASR requested for %s but ffmpeg is not on PATH.", path.name)
        tracker.unavailable = True
    else:
        # phase 1 — language detection prepass
        detected_lang, lang_prob = _pick_language(path)
        started = time.perf_counter()
        try:
            # phase 2b — channel split when stereo+
            if n_channels >= 2 and settings.asr.split_channels_when_multi:
                log.info("ASR using channel-split: %s has %d channels", path.name, n_channels)
                result = transcribe_channels(
                    path,
                    language=detected_lang,
                    channel_count=n_channels,
                )
            else:
                log.info("ASR using simple engine: %s (%d channel%s)",
                         path.name, n_channels, "s" if n_channels != 1 else "")
                result = transcribe(path, language=detected_lang, engine=settings.asr.engine)
            tracker.ran = bool(result and result.text)
        except Exception as exc:  # noqa: BLE001
            log.error("ASR failed for %s: %s", path, exc)
            tracker.failed = True
            result = None
        duration_ms = int((time.perf_counter() - started) * 1000)

    # speaker-aware text when 2+ speakers were detected (channel-split or diarized);
    # plain joined text otherwise. Channel-split/diarized recordings benefit from
    # speaker labels because "[CHANNEL_0] said X / [CHANNEL_1] said Y" disambiguates
    # who-said-what for the enrichment LLM.
    transcript = result.text_with_speakers() if result else ""
    sections = [Section(label=None, text=transcript, index=0)]

    asr_info: AsrInfo | None = None
    decision = tracker.decision(success="asr_full")
    if tracker.ran and result is not None:
        asr_info = AsrInfo.for_whisper_run(
            decision,
            detected_lang=result.language or detected_lang,
            duration_ms=duration_ms,
            audio_seconds=result.duration_seconds or duration_seconds,
        )
        asr_info.channels = result.channels
        asr_info.diarized = result.diarized
        asr_info.speakers = result.speakers()
        asr_info.lang_prob = result.language_probability or lang_prob
    elif skip_transcription:
        # we explicitly chose not to transcribe; still record the type
        from dragndoc.meta_store import AsrInfo as _AsrInfo, utc_now_iso
        asr_info = _AsrInfo(
            decision="no_asr",
            done=utc_now_iso(),
            engine="classifier",
            engine_ver=f"classifier source={classification['source']}",
            langs=[],
            audio_seconds=duration_seconds,
            channels=n_channels,
        )

    # always stamp recording_type onto the info (even no_asr cases)
    if asr_info is None:
        from dragndoc.meta_store import AsrInfo as _AsrInfo
        asr_info = _AsrInfo(decision=decision)
    if classification["committed"]:
        asr_info.recording_type = rec_type
    else:
        asr_info.recording_type = "unknown"

    if not transcript:
        if skip_transcription:
            metadata["asr_status"] = f"skipped_{rec_type}"
        elif tracker.unavailable:
            metadata["asr_status"] = "unavailable"
        elif tracker.failed:
            metadata["asr_status"] = "failed"

    doc = ExtractedDoc(
        path=path,
        sections=sections,
        total_sections=None,
        format="audio",
        ocr_used=False,
        ocr_decision="no_ocr",
        asr_used=tracker.ran,
        asr_decision=decision,
        asr_info=asr_info,
        extracted_metadata=metadata,
    )
    # stash the TranscriptionResult + classification for the pipeline to consume
    doc.transcription = result  # type: ignore[attr-defined]
    doc.recording_type_classification = classification  # type: ignore[attr-defined]
    return doc
