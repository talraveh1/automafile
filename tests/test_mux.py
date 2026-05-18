"""Tests for `dnd mux` — Opus + mkvmerge remux pipeline.

The unit tests cover the pure-logic parts (preset selection, language
resolution); a single integration test runs the full encode + dummy
video + mkvmerge mux on a tiny fixture audio file and only runs when
both ffmpeg and mkvmerge are present.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from dragndoc.cli import app
from dragndoc.config import get_settings
from dragndoc.meta_store import AsrInfo, Doc, OcrInfo, get_by_file, relative_to_root, upsert
from dragndoc.metadata.hashing import hash_file
from dragndoc.mux import (
    AudioProbe,
    OpusPreset,
    mkvmerge_available,
    pick_opus_preset,
    probe_audio,
)
from dragndoc.transcribe import ffmpeg_available, ffprobe_available


runner = CliRunner()


# ---------------------------------------------------------------------------
# pure-logic unit tests
# ---------------------------------------------------------------------------


def test_pick_opus_preset_narrowband_mono_uses_voip_at_24k():
    p = pick_opus_preset(AudioProbe(sample_rate=8000, channels=1, duration_seconds=10.0))
    assert p == OpusPreset(application="voip", bitrate="24k")


def test_pick_opus_preset_narrowband_stereo_uses_voip_at_48k():
    p = pick_opus_preset(AudioProbe(sample_rate=8000, channels=2, duration_seconds=10.0))
    assert p == OpusPreset(application="voip", bitrate="48k")


def test_pick_opus_preset_at_threshold_is_wideband():
    """16000 Hz is the threshold itself; the rule is strict less-than."""
    p = pick_opus_preset(AudioProbe(sample_rate=16000, channels=1, duration_seconds=10.0))
    assert p == OpusPreset(application="audio", bitrate="64k")


def test_pick_opus_preset_wideband_stereo_uses_audio_at_96k():
    p = pick_opus_preset(AudioProbe(sample_rate=48000, channels=2, duration_seconds=10.0))
    assert p == OpusPreset(application="audio", bitrate="96k")


def test_pick_opus_preset_unknown_sample_rate_falls_back_to_wideband_mono():
    """`sample_rate=0` (ffprobe couldn't read it) shouldn't pick the VOIP path."""
    p = pick_opus_preset(AudioProbe(sample_rate=0, channels=1, duration_seconds=10.0))
    assert p == OpusPreset(application="audio", bitrate="64k")


def test_resolve_language_prefers_asr_detected_two_letter_to_iso_639_2():
    from dragndoc.cli.mux import _resolve_language

    class _Asr:
        detected_lang = "he"

    class _Doc:
        asr = _Asr()

    assert _resolve_language(_Doc(), default_lang="und") == "heb"


def test_resolve_language_passes_three_letter_codes_through():
    from dragndoc.cli.mux import _resolve_language

    class _Asr:
        detected_lang = "deu"

    class _Doc:
        asr = _Asr()

    assert _resolve_language(_Doc(), default_lang="und") == "deu"


def test_resolve_language_falls_back_when_no_asr_row():
    from dragndoc.cli.mux import _resolve_language

    assert _resolve_language(None, default_lang="und") == "und"


def test_resolve_language_falls_back_on_empty_detected():
    from dragndoc.cli.mux import _resolve_language

    class _Asr:
        detected_lang = ""

    class _Doc:
        asr = _Asr()

    assert _resolve_language(_Doc(), default_lang="und") == "und"


# ---------------------------------------------------------------------------
# integration: full ffmpeg + mkvmerge round-trip on the fixture audio
# ---------------------------------------------------------------------------


needs_ffmpeg = pytest.mark.skipif(
    not (ffmpeg_available() and ffprobe_available()),
    reason="ffmpeg/ffprobe not on PATH",
)
needs_mkvmerge = pytest.mark.skipif(
    not mkvmerge_available(),
    reason="mkvmerge not installed",
)


@needs_ffmpeg
def test_probe_audio_reports_fixture_sample_rate_and_channels(fixtures_dir):
    """ffprobe should see the fixture as 16 kHz mono (≥ 1 s of audio)."""
    probe = probe_audio(fixtures_dir / "_audio.m4a")
    assert probe.sample_rate == 16000
    assert probe.channels == 1
    assert probe.duration_seconds > 0


@needs_ffmpeg
@needs_mkvmerge
def test_mux_one_replaces_audio_with_mkv_and_restores_mtime(tmp_path, fixtures_dir):
    """End-to-end: source mp3 -> Opus + dummy video + SRT muxed into .mkv."""
    from dragndoc.mux import mux_one

    src = tmp_path / "test.m4a"
    shutil.copyfile(fixtures_dir / "_audio.m4a", src)
    srt = tmp_path / "test.srt"
    shutil.copyfile(fixtures_dir / "_subs.srt", srt)

    # tag the source with a recognizable mtime so we can check restoration
    target_mtime = 1_700_000_000.0  # 2023-11-14 22:13:20 UTC, well in the past
    os.utime(src, (target_mtime, target_mtime))

    result = mux_one(src, srt=srt, language="eng")

    assert result.dst_mkv.exists()
    assert result.dst_mkv.suffix == ".mkv"
    # default behavior: source audio is removed
    assert not src.exists()
    assert result.replaced_original is True
    # SRT sidecar stays where it was
    assert srt.exists()
    # mtime restored
    assert abs(result.dst_mkv.stat().st_mtime - target_mtime) < 2.0

    # verify the produced container has the three expected streams
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "stream=codec_type,codec_name",
            "-of", "default=noprint_wrappers=1",
            str(result.dst_mkv),
        ],
        capture_output=True, text=True, check=True,
        encoding="utf-8", errors="replace",
    )
    out = proc.stdout
    assert "codec_type=video" in out
    assert "codec_type=audio" in out
    assert "codec_name=opus" in out
    assert "codec_type=subtitle" in out


@needs_ffmpeg
@needs_mkvmerge
def test_mux_one_keep_original_keeps_both(tmp_path, fixtures_dir):
    from dragndoc.mux import mux_one

    src = tmp_path / "test.m4a"
    shutil.copyfile(fixtures_dir / "_audio.m4a", src)

    result = mux_one(src, srt=None, language="eng", keep_original=True)

    assert result.dst_mkv.exists()
    assert src.exists()
    assert result.replaced_original is False


@needs_ffmpeg
@needs_mkvmerge
def test_mux_one_refuses_overwrite_without_force(tmp_path, fixtures_dir):
    from dragndoc.mux import mux_one

    src = tmp_path / "test.m4a"
    shutil.copyfile(fixtures_dir / "_audio.m4a", src)
    # pre-create a colliding .mkv
    (tmp_path / "test.mkv").write_bytes(b"existing")

    with pytest.raises(FileExistsError):
        mux_one(src, srt=None, language="eng")

    # with force=True it succeeds
    result = mux_one(src, srt=None, language="eng", force=True, keep_original=True)
    assert result.dst_mkv.exists()
    assert result.dst_mkv.stat().st_size > 100  # not the placeholder bytes


@needs_ffmpeg
@needs_mkvmerge
def test_mux_cli_updates_docs_row(docs_root, fixtures_dir):
    """`dnd mux` should rewrite the docs row's path + hash to the new .mkv."""
    src = docs_root / "Inbox" / "call.m4a"
    shutil.copyfile(fixtures_dir / "_audio.m4a", src)
    upsert(Doc(
        path=relative_to_root(src),
        hash=hash_file(src),
        size=src.stat().st_size,
        original=src.name,
        category="אישי",
        summary="seeded",
        ocr=OcrInfo(decision="no_ocr"),
        asr=AsrInfo(decision="asr_full", detected_lang="en"),
    ))

    result = runner.invoke(app, ["mux", str(src), "--language", "eng"])
    assert result.exit_code == 0, result.output

    mkv = src.with_suffix(".mkv")
    assert mkv.exists()
    assert not src.exists()

    doc = get_by_file(mkv)
    assert doc is not None
    assert doc.path.endswith("call.mkv")
    assert doc.hash == hash_file(mkv)
    assert doc.size == mkv.stat().st_size
