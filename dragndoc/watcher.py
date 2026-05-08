"""Watchdog-based observer for the inbox folder."""

from __future__ import annotations

import os
import time
from pathlib import Path
from threading import Lock

from watchdog.events import FileSystemEventHandler
from watchdog.observers.polling import PollingObserver

from dragndoc.config import get_settings
from dragndoc.events import (
    DIGEST_FINISHED,
    DIGEST_STARTED,
    ERROR,
    append as append_event,
)
from dragndoc.log import get_logger
from dragndoc.pipeline import digest_file, format_result_line
from dragndoc.scanner import ContentChanged, NewFile, NoChange, Renamed, reconcile_single
from dragndoc.meta_store import recompute_dups_for_hashes
from dragndoc.treewalk import BLOCK_MARKER_FILENAME, is_in_blocked_subtree
from dragndoc.triage import count as triage_count


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
        self._maybe_process(Path(os.fsdecode(event.src_path)))

    def on_moved(self, event):
        if event.is_directory:
            return
        self._maybe_process(Path(os.fsdecode(event.dest_path)))

    def on_modified(self, event):
        if event.is_directory:
            return
        self._maybe_process(Path(os.fsdecode(event.src_path)))

    def _maybe_process(self, path: Path) -> None:
        settings = get_settings()
        if path.name == BLOCK_MARKER_FILENAME:
            return
        if is_in_blocked_subtree(path, stop_at=settings.docs):
            log.info("Skipping new file under blocked subtree: %s", path)
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
            outcome = reconcile_single(path)
            match outcome:
                case Renamed(row=row, file_hash=_file_hash):
                    log.info("Rename detected for %s -> row id=%s", path, row.id)
                    return
                case NoChange():
                    return
                case ContentChanged(row=row, file_hash=file_hash):
                    self._digest_and_emit(path, file_hash=file_hash)
                    recompute_dups_for_hashes({row.hash, file_hash})
                case NewFile(file_hash=file_hash):
                    self._digest_and_emit(path, file_hash=file_hash)
                    recompute_dups_for_hashes({file_hash})
        except Exception as exc:  # noqa: BLE001
            log.exception("Unhandled error while processing %s: %s", path, exc)
            append_event(ERROR, file=path.name, error=str(exc))
        finally:
            with self._lock:
                self._inflight.discard(path)

    @staticmethod
    def _digest_and_emit(path: Path, *, file_hash: str) -> None:
        log.info("Digesting file: %s", path)
        append_event(DIGEST_STARTED, scope="file", file=path.name)
        result = digest_file(path, file_hash=file_hash)
        line = format_result_line(result)
        if result.error:
            log.error("%s", line)
            append_event(ERROR, file=path.name, error=result.error)
        else:
            log.info("%s", line)
        try:
            ready = triage_count()
        except Exception:  # noqa: BLE001
            ready = 0
        append_event(
            DIGEST_FINISHED,
            scope="file",
            file=path.name,
            succeeded=0 if result.error else 1,
            failed=1 if result.error else 0,
            category=result.category,
            ready_count=ready,
        )

    @staticmethod
    def _wait_for_settle(path: Path) -> None:
        settings = get_settings()
        time.sleep(settings.watch.settle)
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
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    handler = _InboxHandler()
    observer = PollingObserver(timeout=settings.watch.polling)
    observer.schedule(handler, str(settings.inbox_path), recursive=True)
    observer.start()
    log.info("Watching %s (polling every %.1fs)", settings.inbox_path, settings.watch.polling)
    print(f"[dragndoc] Watching {settings.inbox_path}; Ctrl-C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Watcher stopped by user.")
    finally:
        observer.stop()
        observer.join(timeout=10)
