"""Centralized logging setup."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from threading import Lock

from loguru import logger as _loguru_logger

from dragndoc.config import get_settings


_configured = False
_LOG_FORMAT = "{time:YYYY-MM-DD HH:mm:ss} {level:<7} {extra[name]}: {message}{exception}"


class LineRotatingFileSink:
    """File sink that rotates after a configurable number of lines.

    Backups are named ``<base>.1`` (most recent) ... ``<base>.<N-1>`` (oldest);
    the active file is ``<base>``. ``max_files`` is the total number of files
    kept, including the active one — so backups kept = ``max(max_files - 1, 0)``.
    """

    def __init__(
        self,
        filename: Path,
        max_lines: int,
        max_files: int,
        encoding: str = "utf-8",
    ) -> None:
        self.path = filename
        self.max_lines = max_lines
        self.max_files = max_files
        self.encoding = encoding
        self._lock = Lock()
        self._line_count = self._count_existing_lines()
        self._stream = self._open()

    def _open(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        return self.path.open("a", encoding=self.encoding)

    def _count_existing_lines(self) -> int:
        try:
            with self.path.open("rb") as f:
                return sum(1 for _ in f)
        except OSError:
            return 0

    def write(self, message: str) -> None:
        with self._lock:
            self._stream.write(message)
            self._stream.flush()
            self._line_count += max(1, len(message.splitlines()))
            if self.max_lines > 0 and self._line_count >= self.max_lines:
                self._rotate()

    def flush(self) -> None:
        with self._lock:
            self._stream.flush()

    def stop(self) -> None:
        with self._lock:
            self._stream.close()

    def _rotate(self) -> None:
        self._stream.close()

        base = self.path
        backups_to_keep = max(self.max_files - 1, 0)

        # Drop anything beyond what we want to keep.
        for stale in base.parent.glob(base.name + ".*"):
            try:
                idx = int(stale.name.rsplit(".", 1)[-1])
            except ValueError:
                continue
            if idx > backups_to_keep:
                stale.unlink(missing_ok=True)

        # Shift remaining backups: .N-1 -> .N, ..., .1 -> .2.
        for i in range(backups_to_keep - 1, 0, -1):
            src = base.with_name(base.name + f".{i}")
            dst = base.with_name(base.name + f".{i + 1}")
            if src.exists():
                src.replace(dst)

        # Move the current file to .1, or just drop it if no backups are kept.
        if base.exists():
            if backups_to_keep >= 1:
                base.replace(base.with_name(base.name + ".1"))
            else:
                base.unlink(missing_ok=True)

        self._line_count = 0
        self._stream = self._open()


class LoguruForwardingHandler(logging.Handler):
    """Forward stdlib logging records into the configured Loguru sinks."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level: str | int = record.levelname
            try:
                _loguru_logger.level(record.levelname)
            except ValueError:
                level = record.levelno

            _loguru_logger.bind(name=record.name).opt(
                exception=record.exc_info,
            ).log(level, record.getMessage())
        except Exception:  # noqa: BLE001
            self.handleError(record)


def setup_logging(level: str | None = None) -> None:
    global _configured
    if _configured:
        return
    settings = get_settings()
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    log_level = level or settings.logs.level
    _loguru_logger.remove()
    _loguru_logger.add(
        sys.stderr,
        level=log_level,
        format=_LOG_FORMAT,
        backtrace=False,
        diagnose=False,
        colorize=False,
    )
    try:
        _loguru_logger.add(
            LineRotatingFileSink(
                settings.logs_dir / "dragndoc.log",
                max_lines=settings.logs.max_lines,
                max_files=settings.logs.max_files,
            ),
            level=log_level,
            format=_LOG_FORMAT,
            backtrace=False,
            diagnose=False,
            colorize=False,
        )
    except OSError as exc:
        _loguru_logger.bind(name=__name__).warning("File logging disabled: {}", exc)
    root = logging.getLogger()
    root.handlers = [LoguruForwardingHandler()]
    root.setLevel(log_level)
    _configured = True


def get_logger(name: str) -> logging.Logger:
    setup_logging()
    return logging.getLogger(name)


def log_path() -> Path:
    return get_settings().logs_dir / "dragndoc.log"
