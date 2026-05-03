"""Windows toast primitive with debouncing; falls back to stdout.

Used only by ``automafile.toaster``. The pipeline never imports this
directly — it appends to the events journal and the toaster process
renders the toasts.
"""

from __future__ import annotations

import threading
import time
from typing import Optional

from automafile.log import get_logger


log = get_logger(__name__)


_DEBOUNCE_SECONDS = 5.0


class Notifier:
    """Single-process notifier; coalesces bursts within ``_DEBOUNCE_SECONDS``."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: list[tuple[str, str]] = []
        self._timer: Optional[threading.Timer] = None
        self._toaster = self._init_toaster()

    @staticmethod
    def _init_toaster():
        try:
            from windows_toasts import WindowsToaster
            return WindowsToaster("Automafile")
        except Exception:
            return None

    def notify(self, title: str, body: str) -> None:
        with self._lock:
            self._pending.append((title, body))
            if self._timer is None:
                self._timer = threading.Timer(_DEBOUNCE_SECONDS, self._flush)
                self._timer.daemon = True
                self._timer.start()

    def _flush(self) -> None:
        with self._lock:
            pending = list(self._pending)
            self._pending.clear()
            self._timer = None
        if not pending:
            return
        if len(pending) == 1:
            self._send(*pending[0])
        else:
            count = len(pending)
            sample = pending[-1][0]
            self._send("Automafile", f"Processed {count} files; latest: {sample}")

    def _send(self, title: str, body: str) -> None:
        if self._toaster is not None:
            try:
                from windows_toasts import Toast
                toast = Toast()
                toast.text_fields = [title, body]
                self._toaster.show_toast(toast)
                return
            except Exception as exc:  # noqa: BLE001
                log.debug("Toast send failed: %s", exc)
        print(f"[notify] {title}: {body}")

    def close(self) -> None:
        with self._lock:
            if self._timer:
                self._timer.cancel()
                self._timer = None
        self._flush()
