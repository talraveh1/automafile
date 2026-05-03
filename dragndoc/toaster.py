"""Tail the events journal, fire Windows toasts, and host a tray icon.

Standalone consumer process: invoke via ``dnd toaster``. Runs even
when the pipeline lives in a container, so toasts surface on the host.

Cursor file (``<storage_dir>/toaster.cursor``) tracks the byte offset of
the last consumed event so restarts never miss or duplicate. When the
journal grows past ``COMPACT_THRESHOLD_BYTES`` *and* the cursor has
caught up, the file is truncated and the cursor reset to 0.

The tray icon (pystray) gives a visible "still alive" indication and a
right-click menu — Triage / Log / Exit — plus a status line showing the
most recent event.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from dragndoc.config import get_settings
from dragndoc.events import events_path
from dragndoc.log import get_logger
from dragndoc.notifier import Notifier


log = get_logger(__name__)


CURSOR_FILENAME = "toaster.cursor"
LOG_FILENAME = "dragndoc.log"
COMPACT_THRESHOLD_BYTES = 1_000_000  # 1 MB
POLL_INTERVAL_SECONDS = 1.0
REPO_ROOT = Path(__file__).resolve().parent.parent
TRIAGE_SCRIPT = REPO_ROOT / "scripts" / "triage.cmd"


def cursor_path() -> Path:
    return get_settings().storage_dir / CURSOR_FILENAME


@dataclass
class Cursor:
    offset: int = 0
    size_seen: int = 0

    @classmethod
    def load(cls, path: Path) -> "Cursor":
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls(offset=int(data.get("offset", 0)), size_seen=int(data.get("size_seen", 0)))
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            log.warning("cursor unreadable (%s); restarting from 0", exc)
            return cls()

    def save(self, path: Path) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps({"offset": self.offset, "size_seen": self.size_seen}), encoding="utf-8")
        tmp.replace(path)


@dataclass
class TrayState:
    """Mutable state surfaced via the tray menu so the user can see what's happening."""
    last_notification: Optional[str] = None
    notifications_enabled: bool = True
    action_items: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record_notification(self, title: str, body: str) -> None:
        with self._lock:
            self.last_notification = f"{title}: {body}"

    def set_action_items(self, n: int) -> None:
        with self._lock:
            self.action_items = n

    def has_action_items(self) -> bool:
        with self._lock:
            return self.action_items > 0

    def toggle_notifications(self) -> bool:
        with self._lock:
            self.notifications_enabled = not self.notifications_enabled
            return self.notifications_enabled

    def is_enabled(self) -> bool:
        with self._lock:
            return self.notifications_enabled

    def status_text(self) -> str:
        with self._lock:
            if self.last_notification is None:
                return "No notifications yet"
            return _truncate(self.last_notification, 80)


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _format_toast(event: dict[str, Any]) -> tuple[str, str]:
    """Map an event record to ``(title, body)`` for the toast."""
    kind = event.get("kind", "?")
    if kind == "processed":
        body = f"{event.get('file', '?')} → {event.get('category', '?')}"
        target = event.get("target")
        if target:
            body += f" ({target})"
        return "Drag'n'Doc", body
    if kind == "quarantined":
        return "Sidecar quarantined", f"{event.get('file', '?')} ({event.get('reason', '?')})"
    if kind == "error":
        return "Drag'n'Doc error", f"{event.get('file', '?')}: {event.get('error', '?')}"
    leftover = {k: v for k, v in event.items() if k not in {"ts", "kind"}}
    return "Drag'n'Doc", f"{kind}: {json.dumps(leftover, ensure_ascii=False)}"


def _consume(
    events_file: Path,
    cursor: Cursor,
    notifier: Notifier,
    state: Optional[TrayState] = None,
) -> Cursor:
    """Read new bytes from the journal, fire toasts, return updated cursor.

    When notifications are muted, events are still drained from the journal
    (so the cursor advances and re-enabling doesn't replay a backlog) and
    the status line is still updated — only the toast call is skipped.
    """
    try:
        size = events_file.stat().st_size
    except FileNotFoundError:
        return cursor

    # truncation / rotation: file shrank since we last looked → start over
    if size < cursor.size_seen:
        log.info("events file shrank (%d → %d); resetting cursor", cursor.size_seen, size)
        cursor.offset = 0

    if size <= cursor.offset:
        cursor.size_seen = size
        return cursor

    with events_file.open("rb") as f:
        f.seek(cursor.offset)
        chunk = f.read(size - cursor.offset)

    # only consume whole lines; leave any trailing partial line for next tick
    last_nl = chunk.rfind(b"\n")
    if last_nl < 0:
        cursor.size_seen = size
        return cursor
    consumable = chunk[: last_nl + 1]
    new_offset = cursor.offset + len(consumable)

    enabled = state.is_enabled() if state is not None else True

    for raw in consumable.splitlines():
        if not raw.strip():
            continue
        try:
            event = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            log.warning("skipping malformed event line: %s", exc)
            continue
        title, body = _format_toast(event)
        if enabled:
            notifier.notify(title, body)
        if state is not None:
            state.record_notification(title, body)

    cursor.offset = new_offset
    cursor.size_seen = size
    return cursor


def _maybe_compact(events_file: Path, cursor: Cursor) -> Cursor:
    """If the journal is large and fully consumed, truncate it."""
    try:
        size = events_file.stat().st_size
    except FileNotFoundError:
        return cursor
    if size < COMPACT_THRESHOLD_BYTES or cursor.offset < size:
        return cursor
    try:
        events_file.open("w", encoding="utf-8").close()
    except OSError as exc:
        log.debug("compaction skipped (%s); will retry next tick", exc)
        return cursor
    log.info("compacted events journal (was %d bytes)", size)
    cursor.offset = 0
    cursor.size_seen = 0
    return cursor


# ---------------------------------------------------------------------------
# Tray menu actions
# ---------------------------------------------------------------------------


def _open_log() -> None:
    """Spawn the in-app Tk log viewer as a subprocess."""
    log_file = get_settings().logs_dir / LOG_FILENAME
    # pythonw.exe (windowless) avoids a flashing console; sys.executable is
    # already pythonw when the toaster runs from the Startup shortcut.
    pyw = Path(sys.executable).with_name("pythonw.exe")
    interpreter = pyw if pyw.exists() else Path(sys.executable)
    try:
        subprocess.Popen(
            [str(interpreter), "-m", "dragndoc.log_viewer", str(log_file)],
            cwd=str(REPO_ROOT),
        )
    except OSError as exc:
        log.warning("Could not launch log viewer: %s", exc)


def _find_mintty() -> Optional[str]:
    found = shutil.which("mintty.exe")
    if found:
        return found
    for candidate in (
        Path(r"C:\Program Files\Git\usr\bin\mintty.exe"),
        Path(r"C:\Program Files (x86)\Git\usr\bin\mintty.exe"),
    ):
        if candidate.exists():
            return str(candidate)
    return None


def _launch_triage() -> None:
    """Open a console window running triage.cmd. Prefers Windows Terminal."""
    if not TRIAGE_SCRIPT.exists():
        log.warning("Triage script not found: %s", TRIAGE_SCRIPT)
        return
    # Windows Terminal first (nicer UX); mintty is the fallback. Neither helps
    # with Hebrew BiDi inside Claude Code's TUI — Ink's cursor-addressed output
    # bypasses terminal-side BiDi entirely. cmd /k keeps the window open after
    # the script exits.
    wt = shutil.which("wt.exe")
    mintty = _find_mintty()
    if wt:
        cmd = [wt, "-w", "new", "--focus", "-d", str(REPO_ROOT), "cmd.exe", "/k", str(TRIAGE_SCRIPT)]
    elif mintty:
        # Forward slashes dodge mintty's MSYS-style backslash mangling.
        script = str(TRIAGE_SCRIPT).replace("\\", "/")
        cmd = [mintty, "-h", "always", "--", "cmd.exe", "/k", script]
    else:
        cmd = ["conhost.exe", "cmd.exe", "/k", str(TRIAGE_SCRIPT)]
    try:
        subprocess.Popen(cmd, cwd=str(REPO_ROOT))
    except OSError as exc:
        log.warning("Could not launch triage: %s", exc)


def _count_ready_for_triage() -> int:
    """Files in the inbox tree that have a sidecar — i.e. processed and ready to file.

    Walks recursively under ``<inbox_path>``, skipping the hidden ``.meta/``
    folders and partial-download suffixes. Cheap enough for a 1 s poll on a
    typical inbox; if your inbox grows beyond a few thousand items, swap in
    a watchdog handler instead of polling.
    """
    settings = get_settings()
    inbox = settings.inbox_path
    if not inbox.exists():
        return 0
    meta = settings.meta_subfolder
    skip_suffixes = {".tmp", ".part", ".crdownload"}
    count = 0
    try:
        for p in inbox.rglob("*"):
            if not p.is_file():
                continue
            if meta in p.parts:
                continue
            if p.suffix.lower() in skip_suffixes:
                continue
            sidecar = p.parent / meta / f"{p.name}.md"
            if sidecar.exists():
                count += 1
    except OSError as exc:
        log.debug("inbox count failed: %s", exc)
    return count


# ---------------------------------------------------------------------------
# Tray icon
# ---------------------------------------------------------------------------


def _make_icon_image(red_dot: bool = False):
    """Generate a 64×64 RGBA icon. ``red_dot`` overlays a small red badge
    in the upper-right corner to indicate unhandled events."""
    from PIL import Image, ImageDraw, ImageFont

    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((2, 2, size - 2, size - 2), radius=12, fill=(37, 99, 235, 255))
    try:
        font = ImageFont.truetype("arialbd.ttf", 40)
    except OSError:
        font = ImageFont.load_default()
    text = "A"
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((size - tw) / 2 - bbox[0], (size - th) / 2 - bbox[1] - 2), text, fill=(255, 255, 255, 255), font=font)
    if red_dot:
        # 18px circle in the upper-right corner with a thin white halo so it
        # reads against any tray background
        cx, cy, r = 50, 14, 11
        draw.ellipse((cx - r - 1, cy - r - 1, cx + r + 1, cy + r + 1), fill=(255, 255, 255, 255))
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(220, 38, 38, 255))
    return img


def _run_with_tray(poll_interval: float) -> None:
    """Main entry: pystray icon on the main thread, polling on a background thread."""
    import pystray
    from pystray import MenuItem as Item, Menu

    settings = get_settings()
    settings.storage_dir.mkdir(parents=True, exist_ok=True)

    events_file = events_path()
    cursor_file = cursor_path()
    state = TrayState()
    cursor = Cursor.load(cursor_file)
    notifier = Notifier()
    stop_event = threading.Event()

    # Cache the rendered images so swapping is just an attribute set, not a redraw.
    icon_plain = _make_icon_image(red_dot=False)
    icon_alert = _make_icon_image(red_dot=True)

    def refresh_icon(icon) -> None:
        target = icon_alert if state.has_action_items() else icon_plain
        if icon.icon is not target:
            icon.icon = target

    def poll_loop(icon) -> None:
        nonlocal cursor
        while not stop_event.is_set():
            try:
                # _consume mutates the cursor in place; snapshot the prior
                # state to detect real progress for the save guard.
                prev = (cursor.offset, cursor.size_seen)
                cursor = _consume(events_file, cursor, notifier, state)
                cursor = _maybe_compact(events_file, cursor)
                if (cursor.offset, cursor.size_seen) != prev:
                    cursor.save(cursor_file)
                state.set_action_items(_count_ready_for_triage())
                refresh_icon(icon)
            except Exception as exc:  # noqa: BLE001
                log.exception("Toaster poll error: %s", exc)
            stop_event.wait(poll_interval)

    def on_triage(_icon, _item) -> None:
        _launch_triage()

    def on_log(_icon, _item) -> None:
        _open_log()

    def on_toggle_notifications(_icon, _item) -> None:
        enabled = state.toggle_notifications()
        log.info("Notifications %s via tray.", "enabled" if enabled else "disabled")

    def on_exit(icon, _item) -> None:
        log.info("Toaster exiting via tray menu.")
        stop_event.set()
        icon.stop()

    # Callable menu fields refresh on every open: status line stays current,
    # the Notifications tick reflects the live toggle state.
    menu = Menu(
        Item(lambda _item: state.status_text(), None, enabled=False),
        Menu.SEPARATOR,
        Item("Triage", on_triage),
        Item("Log", on_log),
        Item(
            "Notifications",
            on_toggle_notifications,
            checked=lambda _item: state.is_enabled(),
        ),
        Menu.SEPARATOR,
        Item("Exit", on_exit),
    )

    icon = pystray.Icon("dragndoc", icon_plain, "Drag'n'Doc Toaster", menu=menu)

    poll_thread = threading.Thread(target=poll_loop, args=(icon,), name="toaster-poll", daemon=True)
    poll_thread.start()

    log.info("Toaster watching %s (cursor=%d)", events_file, cursor.offset)
    print(f"[dragndoc] Toaster watching {events_file}; right-click the tray icon to exit.")
    try:
        icon.run()  # blocks until icon.stop()
    finally:
        stop_event.set()
        poll_thread.join(timeout=2.0)
        notifier.close()


def _run_headless(poll_interval: float) -> None:
    """Same loop as ``_run_with_tray`` but without the tray. Used for ``--no-tray``."""
    settings = get_settings()
    settings.storage_dir.mkdir(parents=True, exist_ok=True)

    events_file = events_path()
    cursor_file = cursor_path()
    cursor = Cursor.load(cursor_file)
    notifier = Notifier()

    log.info("Toaster watching %s (cursor=%d, headless)", events_file, cursor.offset)
    print(f"[dragndoc] Toaster watching {events_file}; Ctrl-C to stop.")
    try:
        while True:
            # _consume mutates the cursor in place, so snapshot the prior
            # state before the call to detect real progress.
            prev = (cursor.offset, cursor.size_seen)
            cursor = _consume(events_file, cursor, notifier)
            cursor = _maybe_compact(events_file, cursor)
            if (cursor.offset, cursor.size_seen) != prev:
                cursor.save(cursor_file)
            time.sleep(poll_interval)
    except KeyboardInterrupt:
        log.info("Toaster stopped by user.")
    finally:
        notifier.close()


def run_toaster(poll_interval: float = POLL_INTERVAL_SECONDS, *, tray: bool = True) -> None:
    if tray and sys.platform == "win32":
        _run_with_tray(poll_interval)
    else:
        _run_headless(poll_interval)
