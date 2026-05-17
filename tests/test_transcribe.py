"""Unit tests for the faster-whisper wrapper module.

These tests don't invoke the real Whisper model — they exercise the
shape contract of :func:`dragndoc.transcribe.transcribe` (Path vs.
file-like buffer) by patching the cached ``WhisperModel`` instance.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest


pytest.importorskip("faster_whisper")


from dragndoc import transcribe as transcribe_mod
from dragndoc.transcribe import transcribe, transcribe_bytes


class _FakeSegment:
    def __init__(self, text: str, start: float = 0.0, end: float = 1.0) -> None:
        self.text = text
        self.start = start
        self.end = end


class _FakeInfo:
    language = "he"
    language_probability = 0.99
    duration = 2.0


class _FakeModel:
    """Drop-in replacement that records the source passed and yields fixed segments."""

    def __init__(self) -> None:
        self.calls: list = []

    def transcribe(self, audio, **kwargs):
        self.calls.append((audio, kwargs))
        return iter([_FakeSegment("שלום", 0.0, 0.5), _FakeSegment("עולם", 0.5, 1.0)]), _FakeInfo()

    def detect_language(self, audio):
        return "he", 0.99, [("he", 0.99)]


@pytest.fixture
def fake_model(monkeypatch):
    fake = _FakeModel()
    transcribe_mod._MODEL_CACHE.clear()
    monkeypatch.setattr(transcribe_mod, "_load_model", lambda: fake)
    monkeypatch.setattr(transcribe_mod, "whisper_available", lambda: True)
    yield fake
    transcribe_mod._MODEL_CACHE.clear()


def test_transcribe_path_calls_model_with_string(tmp_path, fake_model):
    fake_audio = tmp_path / "voice.wav"
    fake_audio.write_bytes(b"RIFFfakecontent")
    result = transcribe(fake_audio)
    assert result.text == "שלום עולם"
    assert result.language == "he"
    assert fake_model.calls[0][0] == str(fake_audio)
    # SRT view derived from segments
    assert "שלום" in result.srt
    assert "00:00:00,000" in result.srt


def test_transcribe_buffer_passes_buffer_through(fake_model):
    buf = io.BytesIO(b"RIFFfakecontent")
    result = transcribe(buf)
    assert result.text == "שלום עולם"
    assert result.language == "he"
    # buffer should reach the model verbatim (no copy / conversion)
    assert fake_model.calls[0][0] is buf


def test_transcribe_bytes_equivalent_to_buffer(fake_model):
    payload = b"RIFFfakecontent"
    a = transcribe_bytes(payload)
    b = transcribe(io.BytesIO(payload))
    assert a.text == b.text == "שלום עולם"
    assert a.language == b.language == "he"


def test_transcribe_path_and_buffer_produce_same_transcript(tmp_path, fake_model):
    """Pipe-vs-file equivalence: same bytes → same transcript regardless of source."""
    payload = b"RIFFfakecontent"
    p = tmp_path / "voice.wav"
    p.write_bytes(payload)

    a = transcribe(p)
    b = transcribe(io.BytesIO(payload))
    assert a.text == b.text


def test_transcribe_speaker_label_attaches_to_segments(fake_model):
    """transcribe_channels uses speaker_label to tag each channel's segments."""
    buf = io.BytesIO(b"RIFFfake")
    result = transcribe(buf, speaker_label="CHANNEL_0")
    assert all(s.speaker == "CHANNEL_0" for s in result.segments)
    # speaker label appears in the SRT; the literal "[CHANNEL_0]" substring
    # may be split by bidi control chars when the segment is RTL, so check
    # the label content only
    assert "CHANNEL_0" in result.srt


def test_transcribe_unavailable_raises(monkeypatch):
    """When the engine isn't installed, transcribe surfaces a clean error."""
    monkeypatch.setattr(transcribe_mod, "whisper_available", lambda: False)
    with pytest.raises(RuntimeError, match="not installed"):
        transcribe(Path("anywhere.wav"))


def test_engine_version_includes_model_id():
    from dragndoc.transcribe import engine_version

    v = engine_version()
    assert "faster-whisper" in v
    assert "ivrit-ai/whisper-large-v3-ct2" in v
