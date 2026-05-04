"""Centralized logging setup."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from dragndoc.config import get_settings


_configured = False


def setup_logging(level: str | None = None) -> None:
    global _configured
    if _configured:
        return
    settings = get_settings()
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    log_level = level or settings.log
    handler_console = logging.StreamHandler(stream=sys.stderr)
    handler_console.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    handlers: list[logging.Handler] = [handler_console]
    try:
        handler_file = logging.FileHandler(
            settings.logs_dir / "dragndoc.log",
            encoding="utf-8",
        )
        handler_file.setFormatter(handler_console.formatter)
        handlers.append(handler_file)
    except OSError as exc:
        handler_console.handle(logging.LogRecord(
            name=__name__, level=logging.WARNING, pathname=__file__, lineno=0,
            msg="File logging disabled: %s", args=(exc,), exc_info=None,
        ))
    root = logging.getLogger()
    root.handlers = handlers
    root.setLevel(log_level)
    _configured = True


def get_logger(name: str) -> logging.Logger:
    setup_logging()
    return logging.getLogger(name)


def log_path() -> Path:
    return get_settings().logs_dir / "dragndoc.log"
