"""Single source of truth for runtime settings.

Settings are loaded from ``config.jsonc`` at the repo root. Every key may be
overridden by an environment variable of the same name in upper-case form
(useful for tests); extraction caps use ``EXTRACTION_<KEY>``.
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


# regex helpers for the JSONC stripper
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


class ExtractionSettings(BaseModel):
    """Caps for bounded document text extraction."""

    min_pages: int = 3
    max_pages: int = 5
    per_page_chars: int = 1500
    target_chars: int = 6000


class Settings(BaseModel):
    """Frozen runtime configuration."""

    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "aya-expanse:8b"

    documents_root: Path
    inbox_dir: str = "Inbox"

    tesseract_langs: str = "heb+eng"
    tesseract_bin: str = ""
    tessdata_prefix: str = ""

    watch_settle_seconds: float = 2.0
    watch_polling_interval: float = 5.0
    log_level: str = "INFO"

    ocr_min_text_chars: int = 100
    ocr_min_page_chars: int = 50
    ocr_sparse_page_ratio: float = 0.3
    extraction: ExtractionSettings = Field(default_factory=ExtractionSettings)

    data_dir: Path
    db_path: Path
    logs_dir: Path
    repo_root: Path = REPO_ROOT

    model_config = {"arbitrary_types_allowed": True, "frozen": True}

    @property
    def inbox_path(self) -> Path:
        return self.documents_root / self.inbox_dir


_DEFAULT_DOCUMENTS_ROOT = REPO_ROOT / "documents-root"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    file_cfg = _load_config_file()

    fields: dict[str, Any] = {
        "ollama_url": "http://localhost:11434",
        "ollama_model": "aya-expanse:8b",
        "documents_root": str(_DEFAULT_DOCUMENTS_ROOT),
        "inbox_dir": "Inbox",
        "tesseract_langs": "heb+eng",
        "tesseract_bin": "",
        "tessdata_prefix": "",
        "watch_settle_seconds": 2.0,
        "watch_polling_interval": 5.0,
        "log_level": "INFO",
        "ocr_min_text_chars": 100,
        "ocr_min_page_chars": 50,
        "ocr_sparse_page_ratio": 0.3,
        "data_dir": str(REPO_ROOT / "data"),
        # empty = derive from data_dir
        "db_path": "",
        "logs_dir": "",
    }
    extraction_fields: dict[str, Any] = {
        "min_pages": 3,
        "max_pages": 5,
        "per_page_chars": 1500,
        "target_chars": 6000,
    }

    resolved: dict[str, Any] = {}
    for key, default in fields.items():
        env_val = os.environ.get(key.upper())
        if env_val is not None and env_val != "":
            resolved[key] = _coerce(env_val, default)
        elif key in file_cfg and file_cfg[key] not in (None, ""):
            resolved[key] = file_cfg[key]
        else:
            resolved[key] = default

    extraction_file_cfg = file_cfg.get("extraction") if isinstance(file_cfg.get("extraction"), dict) else {}
    extraction_resolved: dict[str, Any] = {}
    for key, default in extraction_fields.items():
        env_val = os.environ.get(f"EXTRACTION_{key.upper()}")
        if env_val is not None and env_val != "":
            extraction_resolved[key] = _coerce(env_val, default)
        elif key in extraction_file_cfg and extraction_file_cfg[key] not in (None, ""):
            extraction_resolved[key] = extraction_file_cfg[key]
        else:
            extraction_resolved[key] = default

    documents_root = Path(str(resolved.pop("documents_root"))).expanduser().resolve()
    data_dir = Path(str(resolved.pop("data_dir"))).expanduser().resolve()
    db_raw = resolved.pop("db_path")
    logs_raw = resolved.pop("logs_dir")
    db_resolved = Path(str(db_raw)).expanduser().resolve() if db_raw else data_dir / "dragndoc.db"
    logs_dir = Path(str(logs_raw)).expanduser().resolve() if logs_raw else data_dir / "logs"
    return Settings(
        documents_root=documents_root,
        data_dir=data_dir,
        db_path=db_resolved,
        logs_dir=logs_dir,
        extraction=ExtractionSettings(**extraction_resolved),
        **resolved,
    )


def reset_settings() -> None:
    """Drop the cached settings; useful for tests."""
    get_settings.cache_clear()
    # The DB bootstrap memo is keyed by absolute path; clear it so a fresh
    # DATA_DIR (typical in tests) re-runs bootstrap against the new location.
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
