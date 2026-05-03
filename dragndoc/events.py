"""Append-only JSONL event journal consumed by the toaster sidecar.

Decouples notification from the pipeline: the watcher / sidecar writes
events here; a separate `dnd toaster` process tails the file and
fires Windows toasts. Lets the toaster run on the host even when the
pipeline runs in a container.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dragndoc.config import get_settings
from dragndoc.log import get_logger


log = get_logger(__name__)


EVENTS_FILENAME = "events.jsonl"

_lock = threading.Lock()


def events_path() -> Path:
    return get_settings().storage_dir / EVENTS_FILENAME


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def append(kind: str, **fields: Any) -> None:
    """Append one event line. Best-effort — never raises; logs on failure."""
    record: dict[str, Any] = {"ts": _utc_now_iso(), "kind": kind}
    record.update(fields)
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    path = events_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _lock:
            with path.open("a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
    except OSError as exc:
        log.warning("events.append failed (%s): %s", kind, exc)
