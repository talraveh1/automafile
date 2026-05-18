"""Single source of truth for runtime settings.

Settings are loaded from ``config.jsonc`` at the repo root. Nested keys may be
overridden by environment variables that concatenate the group name with the
leaf key, joined by ``_`` and uppercased — e.g. ``watch.settle`` becomes
``WATCH_SETTLE``, ``logs.level`` becomes ``LOGS_LEVEL``, ``ocr.min_text_chars``
becomes ``OCR_MIN_TEXT_CHARS``, top-level keys use their own uppercase form
(``DOCS``, ``INBOX``).
"""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = REPO_ROOT / "config.jsonc"
EXAMPLE_CONFIG_FILE = REPO_ROOT / "config.example.jsonc"


_STRING_RE = re.compile(r'"(?:\\.|[^"\\])*"')
_LINE_COMMENT_RE = re.compile(r"//[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")


def parse_jsonc(text: str) -> dict[str, Any]:
    """Parse JSON-with-comments using only stdlib regex + ``json``."""
    placeholders: list[str] = []

    def stash(m: re.Match[str]) -> str:
        placeholders.append(m.group(0))
        return f"\x00{len(placeholders) - 1}\x00"

    protected = _STRING_RE.sub(stash, text)
    protected = _BLOCK_COMMENT_RE.sub("", protected)
    protected = _LINE_COMMENT_RE.sub("", protected)
    protected = _TRAILING_COMMA_RE.sub(r"\1", protected)
    restored = re.sub(r"\x00(\d+)\x00", lambda m: placeholders[int(m.group(1))], protected)
    return json.loads(restored)


def _load_config_file() -> dict[str, Any]:
    if CONFIG_FILE.exists():
        try:
            return parse_jsonc(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Failed to parse {CONFIG_FILE}: {exc}") from exc
    return {}


def _coerce(value: str, default: Any) -> Any:
    """Coerce an env-string into the type of the default value."""
    if isinstance(default, bool):
        return value.lower() in {"1", "true", "yes", "on"}
    if isinstance(default, int) and not isinstance(default, bool):
        return int(value)
    if isinstance(default, float):
        return float(value)
    return value


class WatchSettings(BaseModel):
    settle: float = 2.0
    polling: float = 5.0
    model_config = {"frozen": True}


class LogsSettings(BaseModel):
    level: str = "INFO"
    max_lines: int = 10000
    max_files: int = 5
    model_config = {"frozen": True}


class OcrSettings(BaseModel):
    max_total_chars: int = 6000
    min_text_chars: int = 100
    min_page_chars: int = 50
    sparse_page_ratio: float = 0.3
    model_config = {"frozen": True}


class OllamaSettings(BaseModel):
    url: str = "http://localhost:11434"
    model: str = "aya-expanse:8b"
    model_config = {"frozen": True}


class TesseractSettings(BaseModel):
    langs: str = "heb+eng"
    bin: str = ""
    prefix: str = ""
    model_config = {"frozen": True}


class AsrSettings(BaseModel):
    model: str = "ivrit-ai/whisper-large-v3-ct2"
    model_dir: str = "data/asr-models"
    device: str = "auto"
    compute_type: str = "float16"
    langs: str = "he,en"
    beam_size: int = 5
    vad_filter: bool = True

    # phase 1 — language detection
    language_detection_seconds: int = 30
    language_detection_min_prob: float = 0.5

    # phase 2 — channel split + sidecars
    split_channels_when_multi: bool = True
    save_srt: bool = True
    save_json: bool = True
    srt_utf8_bom: bool = True
    transcripts_dir: str = "data/transcripts"

    # phase 3 — diarized engine (opt-in)
    engine: str = "simple"            # "simple" | "diarized"
    diarize: bool = False
    hf_token: str = ""
    alignment_models: dict[str, str] = Field(default_factory=dict)
    batch_size: int = 16

    # phase 4 — owner identity, path patterns
    owner_name: str = ""
    owner_aliases: list[str] = Field(default_factory=list)
    path_patterns: list[dict[str, Any]] = Field(default_factory=list)

    # phase 4 — recording-type policy
    transcribe_music: bool = True
    save_srt_for_non_speech: bool = False

    model_config = {"frozen": True, "protected_namespaces": ()}


class DirsSettings(BaseModel):
    """Phase 5 — folder intake classifier settings."""

    classify_on_scan: bool = False
    max_sample_filenames: int = 10
    block_digest_until_classified: bool = False
    model_config = {"frozen": True}


class MuxSettings(BaseModel):
    """`dnd mux` settings — remux audio + transcript into a player-friendly MKV."""

    # threshold separating telephone narrowband from wideband / studio audio;
    # < threshold uses Opus VOIP application, ≥ uses generic audio
    narrowband_sample_rate_threshold: int = 16000

    # Opus bitrates by source bandwidth and channel count (libopus is
    # transparent on speech well below these)
    narrowband_mono_bitrate: str = "24k"
    narrowband_stereo_bitrate: str = "48k"
    wideband_mono_bitrate: str = "64k"
    wideband_stereo_bitrate: str = "96k"

    # MPC-HC's subtitle UI only lights up when the container has a video
    # stream — we mux a 1 fps 2×2 black H.264 dummy track (≈ 1 KB overhead)
    dummy_video_size: int = 2
    dummy_video_fps: int = 1

    # default language tag when none can be inferred from the asr row
    default_language: str = "und"

    # keep the source audio after a successful mux (default: replace it)
    keep_original: bool = False

    # `--compression 0:none` on the subtitle stream — zlib content-compression
    # is the Matroska default and some hardware players still mishandle it
    sub_compression_none: bool = True

    model_config = {"frozen": True}


class Settings(BaseModel):
    """Frozen runtime configuration."""

    docs: Path
    inbox: str = "Inbox"

    logs: LogsSettings = Field(default_factory=LogsSettings)
    watch: WatchSettings = Field(default_factory=WatchSettings)
    ocr: OcrSettings = Field(default_factory=OcrSettings)
    ollama: OllamaSettings = Field(default_factory=OllamaSettings)
    tesseract: TesseractSettings = Field(default_factory=TesseractSettings)
    asr: AsrSettings = Field(default_factory=AsrSettings)
    dirs: DirsSettings = Field(default_factory=DirsSettings)
    mux: MuxSettings = Field(default_factory=MuxSettings)

    data_dir: Path
    db_path: Path
    logs_dir: Path
    repo_root: Path = REPO_ROOT

    model_config = {"arbitrary_types_allowed": True, "frozen": True}

    @property
    def inbox_path(self) -> Path:
        return self.docs / self.inbox


_DEFAULT_DOCS = REPO_ROOT / "documents-root"


_TOP_LEVEL_DEFAULTS: dict[str, Any] = {
    "docs": str(_DEFAULT_DOCS),
    "inbox": "Inbox",
    "data_dir": str(REPO_ROOT / "data"),
    # empty = derive from data_dir
    "db_path": "",
    "logs_dir": "",
}

_SECTIONS: dict[str, tuple[type[BaseModel], dict[str, Any]]] = {
    "logs": (LogsSettings, {"level": "INFO", "max_lines": 10000, "max_files": 5}),
    "watch": (WatchSettings, {"settle": 2.0, "polling": 5.0}),
    "ocr": (
        OcrSettings,
        {
            "max_total_chars": 6000,
            "min_text_chars": 100,
            "min_page_chars": 50,
            "sparse_page_ratio": 0.3,
        },
    ),
    "ollama": (
        OllamaSettings,
        {"url": "http://localhost:11434", "model": "aya-expanse:8b"},
    ),
    "tesseract": (
        TesseractSettings,
        {"langs": "heb+eng", "bin": "", "prefix": ""},
    ),
    "asr": (
        AsrSettings,
        {
            "model": "ivrit-ai/whisper-large-v3-ct2",
            "model_dir": "data/asr-models",
            "device": "auto",
            "compute_type": "float16",
            "langs": "he,en",
            "beam_size": 5,
            "vad_filter": True,
            "language_detection_seconds": 30,
            "language_detection_min_prob": 0.5,
            "split_channels_when_multi": True,
            "save_srt": True,
            "save_json": True,
            "srt_utf8_bom": True,
            "transcripts_dir": "data/transcripts",
            "engine": "simple",
            "diarize": False,
            "hf_token": "",
            "alignment_models": {},
            "batch_size": 16,
            "owner_name": "",
            "owner_aliases": [],
            "path_patterns": [],
            "transcribe_music": True,
            "save_srt_for_non_speech": False,
        },
    ),
    "dirs": (
        DirsSettings,
        {
            "classify_on_scan": False,
            "max_sample_filenames": 10,
            "block_digest_until_classified": False,
        },
    ),
    "mux": (
        MuxSettings,
        {
            "narrowband_sample_rate_threshold": 16000,
            "narrowband_mono_bitrate": "24k",
            "narrowband_stereo_bitrate": "48k",
            "wideband_mono_bitrate": "64k",
            "wideband_stereo_bitrate": "96k",
            "dummy_video_size": 2,
            "dummy_video_fps": 1,
            "default_language": "und",
            "keep_original": False,
            "sub_compression_none": True,
        },
    ),
}


def _resolve_top_level(file_cfg: dict[str, Any]) -> dict[str, Any]:
    resolved: dict[str, Any] = {}
    for key, default in _TOP_LEVEL_DEFAULTS.items():
        env_val = os.environ.get(key.upper())
        if env_val is not None and env_val != "":
            resolved[key] = _coerce(env_val, default)
        elif key in file_cfg and file_cfg[key] not in (None, ""):
            resolved[key] = file_cfg[key]
        else:
            resolved[key] = default
    return resolved


def _resolve_section(name: str, file_cfg: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    raw = file_cfg.get(name)
    sub: dict[str, Any] = raw if isinstance(raw, dict) else {}
    resolved: dict[str, Any] = {}
    for key, default in defaults.items():
        env_val = os.environ.get(f"{name.upper()}_{key.upper()}")
        if env_val is not None and env_val != "":
            resolved[key] = _coerce(env_val, default)
        elif key in sub and sub[key] not in (None, ""):
            resolved[key] = sub[key]
        else:
            resolved[key] = default
    return resolved


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    file_cfg = _load_config_file()
    top = _resolve_top_level(file_cfg)

    sections: dict[str, BaseModel] = {}
    for name, (model, defaults) in _SECTIONS.items():
        sections[name] = model(**_resolve_section(name, file_cfg, defaults))

    docs = Path(str(top.pop("docs"))).expanduser().resolve()
    data_dir = Path(str(top.pop("data_dir"))).expanduser().resolve()
    db_raw = top.pop("db_path")
    logs_raw = top.pop("logs_dir")
    db_path = Path(str(db_raw)).expanduser().resolve() if db_raw else data_dir / "dragndoc.db"
    logs_dir = Path(str(logs_raw)).expanduser().resolve() if logs_raw else data_dir / "logs"

    return Settings(
        docs=docs,
        data_dir=data_dir,
        db_path=db_path,
        logs_dir=logs_dir,
        **sections,
        **top,
    )


def reset_settings() -> None:
    """Drop the cached settings; useful for tests."""
    get_settings.cache_clear()
    try:
        from dragndoc.db import reset_bootstrap_cache
        reset_bootstrap_cache()
    except ImportError:
        pass


def ensure_config_file() -> bool:
    """Create ``config.jsonc`` from ``config.example.jsonc`` if missing."""
    if CONFIG_FILE.exists():
        return False
    if not EXAMPLE_CONFIG_FILE.exists():
        return False
    CONFIG_FILE.write_text(EXAMPLE_CONFIG_FILE.read_text(encoding="utf-8"), encoding="utf-8")
    return True
