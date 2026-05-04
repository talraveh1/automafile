"""Centralized logging setup."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from dragndoc.config import get_settings


_configured = False


class LineRotatingFileHandler(logging.FileHandler):
    """File handler that rotates after a configurable number of lines.

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
        super().__init__(filename, encoding=encoding)
        self.max_lines = max_lines
        self.max_files = max_files
        self._line_count = self._count_existing_lines()

    def _count_existing_lines(self) -> int:
        try:
            with open(self.baseFilename, "rb") as f:
                return sum(1 for _ in f)
        except OSError:
            return 0

    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        try:
            msg = self.format(record)
            self._line_count += msg.count("\n") + 1
            if self.max_lines > 0 and self._line_count >= self.max_lines:
                self._rotate()
        except Exception:  # noqa: BLE001
            self.handleError(record)

    def _rotate(self) -> None:
        if self.stream is not None:
            self.stream.close()
            self.stream = None  # type: ignore[assignment]

        base = Path(self.baseFilename)
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
        if backups_to_keep >= 1:
            base.replace(base.with_name(base.name + ".1"))
        else:
            base.unlink(missing_ok=True)

        self._line_count = 0
        self.stream = self._open()


def setup_logging(level: str | None = None) -> None:
    global _configured
    if _configured:
        return
    settings = get_settings()
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    log_level = level or settings.logs.level
    handler_console = logging.StreamHandler(stream=sys.stderr)
    handler_console.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    handlers: list[logging.Handler] = [handler_console]
    try:
        handler_file = LineRotatingFileHandler(
            settings.logs_dir / "dragndoc.log",
            max_lines=settings.logs.max_lines,
            max_files=settings.logs.max_files,
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
