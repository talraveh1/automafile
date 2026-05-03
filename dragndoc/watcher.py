"""Watchdog-based observer for the inbox folder."""

from __future__ import annotations

import time
from pathlib import Path
from threading import Lock

from watchdog.events import FileSystemEventHandler
from watchdog.observers.polling import PollingObserver

from automafile.config import get_settings
from automafile.log import get_logger
from automafile.events import append as append_event
from automafile.pipeline import format_result_line, process_file


log = get_logger(__name__)


class _InboxHandler(FileSystemEventHandler):
    """File-system event handler that processes new and moved-in files."""

    def __init__(self) -> None:
        super().__init__()
        self._inflight: set[Path] = set()
        self._lock = Lock()

    def on_created(self, event):
        if event.is_directory:
            return
        self._maybe_process(Path(event.src_path))

    def on_moved(self, event):
        if event.is_directory:
            return
        self._maybe_process(Path(event.dest_path))

    def on_modified(self, event):
        if event.is_directory:
            return
        # ignore modifications: we only care about new arrivals to keep things idempotent

    def _maybe_process(self, path: Path) -> None:
        settings = get_settings()
        if path.parent.name == settings.meta_subfolder:
            return
        if path.suffix.lower() in {".tmp", ".part", ".crdownload"}:
            return
        with self._lock:
            if path in self._inflight:
                return
            self._inflight.add(path)
        try:
            self._wait_for_settle(path)
            if not path.exists():
                return
            log.info("Processing new file: %s", path)
            result = process_file(path)
            log.info(format_result_line(result))
            if not result.error:
                append_event(
                    "processed",
                    file=path.name,
                    category=result.category,
                    target=result.metadata_target,
                )
        except Exception as exc:  # noqa: BLE001
            log.exception("Unhandled error while processing %s: %s", path, exc)
        finally:
            with self._lock:
                self._inflight.discard(path)

    @staticmethod
    def _wait_for_settle(path: Path) -> None:
        settings = get_settings()
        time.sleep(settings.watch_settle_seconds)
        last_size = -1
        for _ in range(10):
            try:
                size = path.stat().st_size
            except FileNotFoundError:
                return
            if size == last_size and size > 0:
                return
            last_size = size
            time.sleep(0.5)


def run_watcher() -> None:
    """Foreground loop. Press Ctrl-C to stop."""
    settings = get_settings()
    settings.inbox_path.mkdir(parents=True, exist_ok=True)
    settings.scan_dir.mkdir(parents=True, exist_ok=True)

    handler = _InboxHandler()
    observer = PollingObserver(timeout=settings.watch_polling_interval)
    observer.schedule(handler, str(settings.inbox_path), recursive=True)
    observer.start()
    log.info("Watching %s (polling every %.1fs)", settings.inbox_path, settings.watch_polling_interval)
    print(f"[automafile] Watching {settings.inbox_path}; Ctrl-C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Watcher stopped by user.")
    finally:
        observer.stop()
        observer.join(timeout=10)
