"""Remux audio + transcript into a player-friendly MKV.

Wraps ``ffmpeg`` (Opus encode + dummy black-pixel H.264) and
``mkvmerge`` (Matroska container with the SRT subtitle stream attached
``--compression 0:none``). The result is a single ``.mkv`` next to the
source audio, with original ``mtime`` (and ``ctime`` on Windows)
restored so directory views still sort the recording by its real
recording time, not the mux time.

Default behaviour replaces the source audio file once the mux
succeeds; pass ``keep_original=True`` to keep both.

Why the dummy video — MPC-HC's subtitle UI only lights up when the
container has a video stream. The 2×2 1 fps black H.264 track is the
standard workaround (≈ 1 KB overhead).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from dragndoc.config import get_settings
from dragndoc.log import get_logger
from dragndoc.transcribe import ffprobe_available, ffprobe_streams


log = get_logger(__name__)


# ---------------------------------------------------------------------------
# external-tool discovery
# ---------------------------------------------------------------------------


def mkvmerge_path() -> Path | None:
    """Resolve mkvmerge.exe — PATH first, then the standard MKVToolNix prefix."""
    found = shutil.which("mkvmerge")
    if found:
        return Path(found)
    # winget / MKVToolNix installer default location on Windows
    for guess in (
        Path("C:/Program Files/MKVToolNix/mkvmerge.exe"),
        Path("C:/Program Files (x86)/MKVToolNix/mkvmerge.exe"),
    ):
        if guess.exists():
            return guess
    return None


def mkvmerge_available() -> bool:
    return mkvmerge_path() is not None


def mkvmerge_version() -> str:
    p = mkvmerge_path()
    if not p:
        return ""
    proc = subprocess.run(
        [str(p), "--version"],
        capture_output=True, text=True, check=False,
        encoding="utf-8", errors="replace",
    )
    return (proc.stdout or "").splitlines()[0].strip() if proc.returncode == 0 else ""


# ---------------------------------------------------------------------------
# audio probe + preset selection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AudioProbe:
    sample_rate: int
    channels: int
    duration_seconds: float


@dataclass(frozen=True)
class OpusPreset:
    application: str          # "voip" | "audio"
    bitrate: str              # e.g. "24k", "96k"


def probe_audio(path: Path) -> AudioProbe:
    """Read sample_rate, channels, and duration_seconds from the first audio stream."""
    if not ffprobe_available():
        raise RuntimeError("ffprobe is not installed or on PATH.")
    streams = ffprobe_streams(path)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if not audio:
        raise RuntimeError(f"No audio stream in {path}")
    sample_rate = int(audio.get("sample_rate") or 0)
    channels = int(audio.get("channels") or 1)
    # `format.duration` is the most reliable; fall back to stream-level duration
    duration = 0.0
    fmt_dur = audio.get("duration")  # may be set on the stream
    if fmt_dur not in (None, "", "N/A"):
        try:
            duration = float(fmt_dur)
        except (TypeError, ValueError):
            pass
    if duration == 0.0:
        # fallback — pull duration from format via a second ffprobe call
        proc = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(path),
            ],
            capture_output=True, text=True, check=False,
            encoding="utf-8", errors="replace",
        )
        if proc.returncode == 0:
            try:
                duration = float((proc.stdout or "").strip() or 0.0)
            except ValueError:
                duration = 0.0
    return AudioProbe(sample_rate=sample_rate, channels=channels, duration_seconds=duration)


def pick_opus_preset(probe: AudioProbe) -> OpusPreset:
    """Pick Opus application + bitrate from sample-rate and channel count.

    Narrowband phone-call recordings (< 16 kHz) get the VOIP preset
    (heavier speech tuning, lower bitrate); everything else uses the
    generic audio preset.
    """
    s = get_settings().mux
    narrowband = probe.sample_rate > 0 and probe.sample_rate < s.narrowband_sample_rate_threshold
    if narrowband:
        bitrate = s.narrowband_stereo_bitrate if probe.channels >= 2 else s.narrowband_mono_bitrate
        return OpusPreset(application="voip", bitrate=bitrate)
    bitrate = s.wideband_stereo_bitrate if probe.channels >= 2 else s.wideband_mono_bitrate
    return OpusPreset(application="audio", bitrate=bitrate)


# ---------------------------------------------------------------------------
# encode / mux pipeline
# ---------------------------------------------------------------------------


def _encode_opus(src_audio: Path, dst_opus: Path, preset: OpusPreset) -> None:
    """Encode ``src_audio`` to Opus via ffmpeg using the picked preset."""
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(src_audio),
        "-vn",
        "-c:a", "libopus",
        "-application", preset.application,
        "-b:a", preset.bitrate,
        str(dst_opus),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg Opus encode failed: {proc.stderr.strip() or 'unknown error'}")


def _build_dummy_video(dst_mkv: Path, duration_seconds: float) -> None:
    """Render a tiny black H.264 dummy video (one frame per second, ≈ 1 KB total).

    MPC-HC's subtitle UI ignores subtitle streams in audio-only Matroska
    containers; a token video track flips that off.
    """
    s = get_settings().mux
    # at least 1 s of video so mkvmerge doesn't choke on a zero-length input
    duration = max(1.0, duration_seconds)
    size = max(2, s.dummy_video_size)
    fps = max(1, s.dummy_video_fps)
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi",
        "-i", f"color=c=black:s={size}x{size}:d={duration}:r={fps}",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-tune", "stillimage",
        "-pix_fmt", "yuv420p",
        str(dst_mkv),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg dummy-video build failed: {proc.stderr.strip() or 'unknown error'}")


def _mkvmerge_mux(
    out_mkv: Path,
    dummy_video: Path,
    opus_audio: Path,
    srt: Path | None,
    language: str,
) -> None:
    """Mux the three streams into ``out_mkv`` with the canonical mkvmerge invocation."""
    s = get_settings().mux
    bin_ = mkvmerge_path()
    if not bin_:
        raise RuntimeError("mkvmerge is not installed or on PATH.")
    cmd: list[str] = [
        str(bin_), "-o", str(out_mkv),
        # dummy video (undefined language, not default)
        "--language", "0:und", "--default-track", "0:no",
        "--no-audio", "--no-subtitles",
        str(dummy_video),
        # audio (tagged with the picked language, default track)
        "--language", f"0:{language}", "--default-track", "0:yes",
        "--no-video", "--no-subtitles",
        str(opus_audio),
    ]
    if srt is not None and srt.exists():
        sub_args: list[str] = ["--language", f"0:{language}", "--default-track", "0:yes"]
        if s.sub_compression_none:
            sub_args += ["--compression", "0:none"]
        # explicit UTF-8 — SRT sidecars are written with BOM elsewhere; this
        # guards against a stray non-BOM file being mis-detected as cp1252
        sub_args += ["--sub-charset", "0:UTF-8"]
        cmd += sub_args + [str(srt)]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"mkvmerge failed: {proc.stderr.strip() or proc.stdout.strip() or 'unknown error'}")


# ---------------------------------------------------------------------------
# timestamp restoration
# ---------------------------------------------------------------------------


def restore_timestamps(dst: Path, src_stat: os.stat_result) -> None:
    """Copy mtime + atime from ``src_stat`` to ``dst``; on Windows also ctime."""
    os.utime(dst, (src_stat.st_atime, src_stat.st_mtime))
    if sys.platform == "win32":
        try:
            from win32_setctime import setctime  # type: ignore[import-not-found]
        except ImportError:
            log.warning(
                "win32-setctime not installed; .mkv creation time will reflect "
                "the mux time instead of the source's. `pip install win32-setctime` to fix."
            )
            return
        try:
            setctime(str(dst), src_stat.st_ctime)
        except OSError as exc:
            log.warning("Could not restore ctime on %s: %s", dst, exc)


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------


@dataclass
class MuxResult:
    src_audio: Path
    dst_mkv: Path
    srt: Path | None
    src_bytes: int
    dst_bytes: int
    duration_seconds: float
    preset: OpusPreset
    language: str
    replaced_original: bool

    @property
    def bytes_saved(self) -> int:
        return self.src_bytes - self.dst_bytes


def mux_one(
    src_audio: Path,
    *,
    srt: Path | None = None,
    language: str | None = None,
    force: bool = False,
    keep_original: bool | None = None,
) -> MuxResult:
    """Remux ``src_audio`` (+ optional ``srt`` sidecar) into a sibling .mkv.

    Default replaces the source after a successful mux; pass
    ``keep_original=True`` (or set ``mux.keep_original=True`` in
    ``config.jsonc``) to keep both files.
    """
    if not src_audio.is_file():
        raise FileNotFoundError(f"Source audio not found: {src_audio}")
    if not ffprobe_available():
        raise RuntimeError("ffprobe is not installed or on PATH.")
    if not mkvmerge_available():
        raise RuntimeError(
            "mkvmerge is not installed. Install MKVToolNix: "
            "`winget install MoritzBunkus.MKVToolNix`."
        )

    settings = get_settings()
    keep = settings.mux.keep_original if keep_original is None else keep_original
    lang = language or settings.mux.default_language

    dst_mkv = src_audio.with_suffix(".mkv")
    if dst_mkv.exists() and not force:
        raise FileExistsError(f"Target exists: {dst_mkv} (use -f to overwrite)")

    src_stat = src_audio.stat()
    src_bytes = src_stat.st_size
    probe = probe_audio(src_audio)
    preset = pick_opus_preset(probe)

    with tempfile.TemporaryDirectory(prefix="dnd_mux_") as tmp:
        tmp_path = Path(tmp)
        opus_tmp = tmp_path / "audio.opus"
        video_tmp = tmp_path / "video.mkv"
        out_tmp = tmp_path / "out.mkv"

        log.info("mux: encoding %s -> Opus (%s @ %s)", src_audio.name, preset.application, preset.bitrate)
        _encode_opus(src_audio, opus_tmp, preset)
        log.info("mux: building dummy video for %.1fs", probe.duration_seconds)
        _build_dummy_video(video_tmp, probe.duration_seconds)
        log.info("mux: combining streams via mkvmerge")
        _mkvmerge_mux(out_tmp, video_tmp, opus_tmp, srt, lang)

        # only now overwrite the destination — atomic move from temp
        if dst_mkv.exists():
            dst_mkv.unlink()
        shutil.move(str(out_tmp), str(dst_mkv))

    restore_timestamps(dst_mkv, src_stat)

    replaced = False
    if not keep:
        # keep the SRT sidecar; remove only the source audio
        try:
            src_audio.unlink()
            replaced = True
        except OSError as exc:
            log.warning("Could not remove source %s: %s", src_audio, exc)

    return MuxResult(
        src_audio=src_audio,
        dst_mkv=dst_mkv,
        srt=srt if srt and srt.exists() else None,
        src_bytes=src_bytes,
        dst_bytes=dst_mkv.stat().st_size,
        duration_seconds=probe.duration_seconds,
        preset=preset,
        language=lang,
        replaced_original=replaced,
    )
