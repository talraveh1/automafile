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
from dragndoc.meta_store import delete_by_path, recompute_dups_for_hashes, relative_to_root
from dragndoc.treewalk import opaque_ancestor, topmost_opaque_ancestor
from dragndoc.triage import count as triage_count


log = get_logger(__name__)


class _InboxHandler(FileSystemEventHandler):
    """File-system event handler that processes new and moved-in files."""

    def __init__(self) -> None:
        super().__init__()
        self._inflight: set[Path] = set()
        self._announced_opaque: set[Path] = set()
        self._lock = Lock()

    def on_created(self, event):
        path = Path(os.fsdecode(event.src_path))
        self._log_top_level("Created", path)
        if event.is_directory:
            self._announce_opaque_for(path)
            return
        self._maybe_process(path)

    def on_moved(self, event):
        src = Path(os.fsdecode(event.src_path))
        dest = Path(os.fsdecode(event.dest_path))
        self._log_top_level("Moved", src, dest)
        with self._lock:
            self._announced_opaque.discard(src)
        if event.is_directory:
            self._announce_opaque_for(dest)
            return
        self._maybe_process(dest)

    def on_modified(self, event):
        path = Path(os.fsdecode(event.src_path))
        self._log_top_level("Modified", path)
        if event.is_directory:
            return
        self._maybe_process(path)

    def on_deleted(self, event):
        path = Path(os.fsdecode(event.src_path))
        self._log_top_level("Deleted", path)
        with self._lock:
            self._announced_opaque.discard(path)
        if event.is_directory:
            return
        self._handle_file_deletion(path)

    @staticmethod
    def _handle_file_deletion(path: Path) -> None:
        """Drop the docs row when a file is physically removed.

        ``dnd mv`` updates the row's path *before* the polling cycle observes the
        old path missing, so by the time we get here the row is either already
        gone (mv handled it) or genuinely orphaned. The triage row cascades via
        the ON DELETE foreign key, which is what keeps "X files awaiting triage"
        honest without a separate hook.
        """
        if opaque_ancestor(path, stop_at=get_settings().docs) is not None:
            return
        rel = relative_to_root(path)
        if delete_by_path(rel):
            log.info("Dropped metadata for deleted file: %s", path)

    @staticmethod
    def _log_top_level(action: str, src: Path, dest: Path | None = None) -> None:
        inbox = get_settings().inbox_path
        if src.parent != inbox and (dest is None or dest.parent != inbox):
            return
        if dest is not None:
            log.info("%s: %s -> %s", action, src, dest)
        else:
            log.info("%s: %s", action, src)

    def _announce_opaque_for(self, path: Path) -> None:
        """Log the opaque-subtree skip once per outermost opaque subtree."""
        opaque = topmost_opaque_ancestor(path, stop_at=get_settings().docs)
        if opaque is None:
            return
        with self._lock:
            if opaque in self._announced_opaque:
                return
            self._announced_opaque.add(opaque)
        log.info("Skipping opaque subtree: %s", opaque)

    def _maybe_process(self, path: Path) -> None:
        settings = get_settings()
        if path.name.startswith("."):
            return
        if opaque_ancestor(path, stop_at=settings.docs) is not None:
            self._announce_opaque_for(path)
            return
        if path.suffix.lower() in {".tmp", ".part", ".crdownload"}:
            return
        with self._lock:
            if path in self._inflight:
                return
            # watchdog can emit create/modify/move bursts for the same file
            self._inflight.add(path)
        try:
            self._wait_for_settle(path)
            if not path.exists():
                return
            outcome = reconcile_single(path)
            match outcome:
                case Renamed(row=row, file_hash=_file_hash):
                    # a pure rename keeps the existing metadata row, so there is nothing to redigest
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
        except FileNotFoundError:
            log.info("File vanished before processing: %s", path)
        except PermissionError as exc:
            log.warning("Permission denied while processing %s: %s", path, exc)
            append_event(ERROR, file=path.name, error=str(exc))
        except OSError as exc:
            log.warning("OS error while processing %s: %s", path, exc)
            append_event(ERROR, file=path.name, error=str(exc))
        except Exception as exc:  # noqa: BLE001
            log.exception("Unhandled error while processing %s: %s", path, exc)
            append_event(ERROR, file=path.name, error=str(exc))
        finally:
            with self._lock:
                self._inflight.discard(path)

    @staticmethod
    def _digest_and_emit(path: Path, *, file_hash: str) -> None:
        log.info("Digesting file: %s", path)
        # events keep the toaster and tray status decoupled from the watcher
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
                # require two matching non-zero sizes so we do not digest a file mid-copy
                return
            last_size = size
            time.sleep(0.5)


def _reconcile_inbox_orphans() -> int:
    """Drop bogus docs rows under the inbox so the "awaiting triage" count is honest.

    Two kinds of rows get cleared on startup so they don't keep inflating the
    queue across watcher restarts:

    - files that vanished from disk while the watcher was offline
    - files that live inside an opaque subtree (e.g. an unpacked ``.venv``);
      these should never have been digested but may linger from earlier runs
      that pre-date the opaque-skipping logic

    Triage rows cascade via the docs FK.
    """
    from dragndoc.db import transaction

    settings = get_settings()
    inbox_prefix = settings.inbox.rstrip("/") + "/"
    docs_root = settings.docs
    with transaction() as conn:
        rows = conn.execute(
            "SELECT id, path FROM docs WHERE path LIKE ?",
            (f"{inbox_prefix}%",),
        ).fetchall()
        drop_ids: list[int] = []
        for row in rows:
            full = docs_root / row["path"]
            if not full.exists():
                drop_ids.append(row["id"])
                continue
            if opaque_ancestor(full, stop_at=docs_root) is not None:
                drop_ids.append(row["id"])
        if not drop_ids:
            return 0
        placeholders = ",".join("?" * len(drop_ids))
        conn.execute(f"DELETE FROM docs WHERE id IN ({placeholders})", drop_ids)
    return len(drop_ids)


def run_watcher() -> None:
    """Foreground loop. Press Ctrl-C to stop."""
    settings = get_settings()
    settings.inbox_path.mkdir(parents=True, exist_ok=True)
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    removed = _reconcile_inbox_orphans()
    if removed:
        log.info("Startup reconciliation: dropped %d orphan inbox row(s)", removed)

    handler = _InboxHandler()
    observer = PollingObserver(timeout=settings.watch.polling)
    observer.schedule(handler, str(settings.inbox_path), recursive=True)
    observer.start()
    log.info("Watching %s (polling every %.1fs)", settings.inbox_path, settings.watch.polling)
    print(f"[dragndoc] Watching {settings.inbox_path}; Ctrl-C to stop.")
    from dragndoc.runtime import write_heartbeat

    try:
        while True:
            # heartbeat lets the host toaster see the container watcher is alive
            # without trying to resolve its (container-namespace) PID
            write_heartbeat()
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Watcher stopped by user.")
    finally:
        observer.stop()
        observer.join(timeout=10)
