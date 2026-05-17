"""Integration tests for the audio + video extractors.

These tests rely on real ``ffmpeg``/``ffprobe`` to be on PATH and use small
synthetic media fixtures generated under ``tests/fixtures/``. They skip
gracefully when ffmpeg or faster-whisper is unavailable, so CI runs that
don't install the system prereqs still pass.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest


pytest.importorskip("faster_whisper")
if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
    pytest.skip("ffmpeg/ffprobe not on PATH", allow_module_level=True)


FIXTURES = Path(__file__).parent / "fixtures"


def _require_fixture(name: str) -> Path:
    p = FIXTURES / name
    if not p.exists():
        pytest.skip(f"missing fixture: {p}")
    return p


def test_video_extractor_uses_subtitle_when_present(monkeypatch):
    """A video with embedded text subs takes the subtitle path; no Whisper call."""
    from dragndoc.extractors import video as video_ext
    from dragndoc import transcribe as transcribe_mod

    fixture = _require_fixture("_video_with_subs.mp4")

    calls: list = []

    def _spy(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("transcribe must not be called when subtitle path is used")

    monkeypatch.setattr(transcribe_mod, "transcribe_bytes", _spy)

    doc = video_ext.extract(fixture)
    assert doc.asr_used is True
    assert doc.asr_decision == "asr_subtitle"
    assert doc.asr_info is not None
    assert doc.asr_info.engine == "subtitle"
    assert "hello world" in doc.text.lower()
    assert calls == []  # never reached Whisper


def test_video_extractor_falls_back_to_asr_without_text_subs(monkeypatch):
    """A video with no text subtitle drops down to the audio-extract path."""
    from dragndoc.extractors import video as video_ext
    from dragndoc import transcribe as transcribe_mod
    from dragndoc.transcribe import TranscriptionResult, TranscriptSegment

    fixture = _require_fixture("_video.mp4")

    calls: list = []

    def _stub_transcribe_bytes(audio_bytes, langs=None, language=None, engine="simple"):
        calls.append(len(audio_bytes))
        seg = TranscriptSegment(start=0.0, end=1.0, text="stub transcript")
        return TranscriptionResult(
            segments=[seg],
            text="stub transcript",
            srt="1\n00:00:00,000 --> 00:00:01,000\nstub transcript\n",
            language="en",
            channels=1,
            engine="faster-whisper",
        )

    monkeypatch.setattr(transcribe_mod, "transcribe_bytes", _stub_transcribe_bytes)
    # bypass the import in video.py's namespace too
    monkeypatch.setattr(video_ext, "transcribe_bytes", _stub_transcribe_bytes)
    monkeypatch.setattr(video_ext, "whisper_available", lambda: True)

    doc = video_ext.extract(fixture)
    assert doc.asr_used is True
    assert doc.asr_decision == "asr_full"
    assert doc.asr_info is not None
    assert doc.asr_info.engine == "faster-whisper"
    assert calls and calls[0] > 0, "ffmpeg should pipe wav bytes to transcribe_bytes"
    assert "stub transcript" in doc.text


def test_audio_extractor_handles_engine_unavailable(monkeypatch):
    """When whisper is unavailable, audio extractor still returns a clean doc."""
    from dragndoc.extractors import audio as audio_ext

    fixture = _require_fixture("_audio.m4a")
    monkeypatch.setattr(audio_ext, "whisper_available", lambda: False)
    doc = audio_ext.extract(fixture)
    assert doc.asr_used is False
    assert doc.asr_decision == "asr_unavailable"
    # asr_info is stamped with the failure decision so downstream still gets a row
    assert doc.asr_info is not None
    assert doc.asr_info.decision == "asr_unavailable"
    # mutagen-derived metadata should still be present (length, codec, etc.)
    assert any(key.startswith("audio_") for key in doc.extracted_metadata)
