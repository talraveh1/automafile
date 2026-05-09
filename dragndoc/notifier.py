"""Windows toast primitive with debouncing; falls back to stdout.

Used only by ``dragndoc.toaster``. The pipeline never imports this
directly — it appends to the events journal and the toaster process
renders the toasts.
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Optional

from dragndoc.log import get_logger


log = get_logger(__name__)


_DEBOUNCE_SECONDS = 5.0

# aumid = Application User Model ID. Must match the value registered by
# ``dnd toaster install`` under HKCU\Software\Classes\AppUserModelId\<AUMID>.
# windows uses it to attribute toasts and look up the display name
AUMID = "DragnDoc.Toaster"
AUMID_REG_PATH = rf"Software\Classes\AppUserModelId\{AUMID}"


def _aumid_is_registered() -> bool:
    """True iff the HKCU AUMID key exists. Pure read; no admin required."""
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUMID_REG_PATH):
            return True
    except (FileNotFoundError, OSError):
        return False


class Notifier:
    """Single-process notifier; coalesces bursts within ``_DEBOUNCE_SECONDS``."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: list[tuple[str, str]] = []
        self._timer: Optional[threading.Timer] = None
        self._toaster = self._init_toaster()

    @staticmethod
    def _init_toaster():
        """Create a WindowsToaster bound to our registered AUMID.

        The AUMID must be registered in the registry (done by
        ``dnd toaster install``); without it Windows accepts every toast
        but silently drops them. We don't auto-register here because that
        belongs in the install flow, not at every toaster startup."""
        try:
            from windows_toasts import WindowsToaster
        except Exception as exc:  # noqa: BLE001
            log.warning("windows-toasts import failed (%s); toasts will print to stdout.", exc)
            return None
        if not _aumid_is_registered():
            log.warning(
                "AUMID %r is not registered in HKCU\\%s; "
                "Windows will silently drop toasts. Run: python scripts\\toaster.py",
                AUMID, AUMID_REG_PATH,
            )
            return None
        try:
            return WindowsToaster(AUMID)
        except Exception as exc:  # noqa: BLE001
            log.warning("WindowsToaster(%r) failed (%s); toasts will print to stdout.", AUMID, exc)
            return None

    def notify(self, title: str, body: str) -> None:
        with self._lock:
            self._pending.append((title, body))
            if self._timer is None:
                # debounce bursts so a scan does not produce one toast per file
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
            self._send("Drag'n'Doc", f"Processed {count} files; latest: {sample}")

    def _send(self, title: str, body: str) -> None:
        if self._toaster is not None:
            try:
                from windows_toasts import Toast
                toast = Toast()
                toast.text_fields = [title, body]
                self._toaster.show_toast(toast)
                return
            except Exception as exc:  # noqa: BLE001
                log.warning("Toast send failed (%s); falling back to stdout.", exc)
        print(f"[notify] {title}: {body}")

    def close(self) -> None:
        with self._lock:
            if self._timer:
                self._timer.cancel()
                self._timer = None
        self._flush()
