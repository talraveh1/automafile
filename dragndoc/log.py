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

    Backups are named ``<stem>.1<suffix>`` (most recent) ... ``<stem>.<N-1><suffix>``
    (oldest) so the original extension is preserved (e.g. ``dragndoc.1.log``);
    the active file is ``<base>``. ``max_files`` is the total number of files
    kept, including the active one — so backups kept = ``max(max_files - 1, 0)``.

    The line counter only tracks lines written by *this* process, so short-lived
    CLI invocations effectively never rotate — only long-lived writers (the
    watcher) trip rotation, which avoids cross-process rename races. Any
    remaining race (rename while another process has the file open) is caught
    and the rotation is skipped — the next attempt will catch up.
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
        self._line_count = 0
        self._migrate_legacy_backups()
        self._stream = self._open()

    def _open(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        return self.path.open("a", encoding=self.encoding)

    def _backup_path(self, idx: int) -> Path:
        return self.path.with_name(f"{self.path.stem}.{idx}{self.path.suffix}")

    def _migrate_legacy_backups(self) -> None:
        # historical layout appended ``.N`` to the full filename (``dragndoc.log.1``);
        # promote those to the new ``<stem>.N<suffix>`` form once, best-effort
        for legacy in self.path.parent.glob(self.path.name + ".*"):
            try:
                idx = int(legacy.name.rsplit(".", 1)[-1])
            except ValueError:
                continue
            try:
                legacy.replace(self._backup_path(idx))
            except OSError:
                pass

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

        # drop anything beyond what we want to keep
        for stale in base.parent.glob(f"{base.stem}.*{base.suffix}"):
            mid = stale.name[len(base.stem) + 1 : -len(base.suffix) or None]
            try:
                idx = int(mid)
            except ValueError:
                continue
            if idx > backups_to_keep:
                stale.unlink(missing_ok=True)

        # shift remaining backups: .N-1 -> .N, ..., .1 -> .2
        for i in range(backups_to_keep - 1, 0, -1):
            src = self._backup_path(i)
            dst = self._backup_path(i + 1)
            if src.exists():
                try:
                    src.replace(dst)
                except OSError:
                    pass

        # move the current file to .1, or just drop it if no backups are kept
        if base.exists():
            try:
                if backups_to_keep >= 1:
                    base.replace(self._backup_path(1))
                else:
                    base.unlink(missing_ok=True)
            except OSError:
                # another process likely holds the file open; defer rotation
                pass

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
