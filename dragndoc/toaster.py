"""Poll the events table, fire Windows toasts, host a tray icon.

Standalone consumer process: invoke via ``dnd toaster``. Runs even
when the pipeline lives in a container, so toasts surface on the host.

Cursor file (``<data_dir>/toaster.cursor``) holds the last consumed
``events.id`` so restarts never miss or duplicate. The first run with a
non-existent cursor jumps to the current latest id (no replaying old
events).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from dragndoc.config import get_settings
from dragndoc.events import fetch_since, latest_id
from dragndoc.log import get_logger
from dragndoc.notifier import Notifier
from dragndoc.process import pid_alive, terminate_pid


log = get_logger(__name__)


CURSOR_FILENAME = "toaster.cursor"
LOG_FILENAME = "dragndoc.log"
POLL_INTERVAL_SECONDS = 1.0
REPO_ROOT = Path(__file__).resolve().parent.parent
TRIAGE_SCRIPT = REPO_ROOT / "scripts" / "triage.cmd"


def cursor_path() -> Path:
    return get_settings().data_dir / CURSOR_FILENAME


@dataclass
class Cursor:
    last_id: int = 0

    @classmethod
    def load(cls, path: Path) -> "Cursor":
        if not path.exists():
            return cls(last_id=0)
        try:
            raw = path.read_text(encoding="utf-8").strip()
            return cls(last_id=int(raw or "0"))
        except (OSError, ValueError) as exc:
            log.warning("cursor unreadable (%s); restarting from 0", exc)
            return cls()

    def save(self, path: Path) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(str(self.last_id), encoding="utf-8")
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
    """Map an event row to ``(title, body)`` for the toast.

    ``event`` is ``{id, ts, kind, payload}`` where ``payload`` is the dict
    of named fields that ``events.append(kind, **fields)`` was called with.
    """
    kind = event.get("kind", "?")
    payload = event.get("payload") or {}
    if kind == "processed":
        body = f"{payload.get('file', '?')} → {payload.get('category', '?')}"
        target = payload.get("target")
        if target:
            body += f" ({target})"
        return "Drag'n'Doc", body
    if kind == "error":
        return "Drag'n'Doc error", f"{payload.get('file', '?')}: {payload.get('error', '?')}"
    return "Drag'n'Doc", f"{kind}: {payload}"


def _consume(cursor: Cursor, notifier: Notifier, state: Optional[TrayState] = None) -> Cursor:
    """Drain new events; fire toasts; return updated cursor.

    When notifications are muted, events are still drained so re-enabling
    doesn't replay a backlog and the status line stays current.
    """
    try:
        rows = fetch_since(cursor.last_id, limit=500)
    except Exception as exc:  # noqa: BLE001
        log.warning("toaster fetch failed: %s", exc)
        return cursor
    if not rows:
        return cursor

    enabled = state.is_enabled() if state is not None else True
    for event in rows:
        title, body = _format_toast(event)
        if enabled:
            try:
                notifier.notify(title, body)
            except Exception as exc:  # noqa: BLE001
                log.warning("notifier.notify failed: %s", exc)
        if state is not None:
            state.record_notification(title, body)
        cursor.last_id = max(cursor.last_id, int(event["id"]))
    return cursor


# ---------------------------------------------------------------------------
# Tray menu actions
# ---------------------------------------------------------------------------


def _open_log() -> None:
    """Spawn the in-app Tk log viewer as a subprocess."""
    log_file = get_settings().logs_dir / LOG_FILENAME
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
    wt = shutil.which("wt.exe")
    mintty = _find_mintty()
    if wt:
        cmd = [wt, "-w", "new", "--focus", "-d", str(REPO_ROOT), "cmd.exe", "/k", str(TRIAGE_SCRIPT)]
    elif mintty:
        script = str(TRIAGE_SCRIPT).replace("\\", "/")
        cmd = [mintty, "-h", "always", "--", "cmd.exe", "/k", script]
    else:
        cmd = ["conhost.exe", "cmd.exe", "/k", str(TRIAGE_SCRIPT)]
    try:
        subprocess.Popen(cmd, cwd=str(REPO_ROOT))
    except OSError as exc:
        log.warning("Could not launch triage: %s", exc)


def _count_ready_for_triage() -> int:
    """Files in the inbox tree that have a metadata row — i.e. processed and ready to file."""
    from dragndoc.db import connect

    settings = get_settings()
    inbox = settings.inbox_path
    if not inbox.exists():
        return 0
    inbox_rel = settings.inbox.rstrip("/") + "/"
    try:
        with connect(readonly=True) as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM docs WHERE path LIKE ?",
                (f"{inbox_rel}%",),
            ).fetchone()
    except Exception as exc:  # noqa: BLE001
        log.debug("inbox count failed: %s", exc)
        return 0
    return int(row["n"] if row else 0)


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
        cx, cy, r = 50, 14, 11
        draw.ellipse((cx - r - 1, cy - r - 1, cx + r + 1, cy + r + 1), fill=(255, 255, 255, 255))
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(220, 38, 38, 255))
    return img


def _initial_cursor(cursor_file: Path) -> Cursor:
    """If the cursor file exists, load it. Otherwise jump to the current latest id."""
    if cursor_file.exists():
        return Cursor.load(cursor_file)
    cursor = Cursor(last_id=latest_id())
    cursor.save(cursor_file)
    return cursor


def _run_with_tray(poll_interval: float) -> None:
    """Main entry: pystray icon on the main thread, polling on a background thread."""
    import pystray
    from pystray import MenuItem as Item, Menu

    settings = get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    cursor_file = cursor_path()
    state = TrayState()
    cursor = _initial_cursor(cursor_file)
    notifier = Notifier()
    stop_event = threading.Event()

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
                prev = cursor.last_id
                cursor = _consume(cursor, notifier, state)
                if cursor.last_id != prev:
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

    log.info("Toaster polling events table (cursor=%d)", cursor.last_id)
    print(f"[dragndoc] Toaster polling events table; right-click the tray icon to exit.")
    try:
        icon.run()
    finally:
        stop_event.set()
        poll_thread.join(timeout=2.0)
        notifier.close()


def _run_headless(poll_interval: float) -> None:
    """Same loop as ``_run_with_tray`` but without the tray. Used for ``--no-tray``."""
    settings = get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    cursor_file = cursor_path()
    cursor = _initial_cursor(cursor_file)
    notifier = Notifier()

    log.info("Toaster polling events table (cursor=%d, headless)", cursor.last_id)
    print(f"[dragndoc] Toaster polling events table; Ctrl-C to stop.")
    try:
        while True:
            prev = cursor.last_id
            cursor = _consume(cursor, notifier)
            if cursor.last_id != prev:
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


# ---------------------------------------------------------------------------
# Lifecycle: PID-file based start / stop / status
# ---------------------------------------------------------------------------


def pid_file() -> Path:
    return get_settings().data_dir / "runtime" / "toaster.pid"


def _write_pid(pid: int) -> None:
    p = pid_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"{pid}\n", encoding="utf-8")


def _read_pid() -> int | None:
    try:
        raw = pid_file().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


def _clear_pid() -> None:
    pid_file().unlink(missing_ok=True)


def status_snapshot() -> dict[str, Any]:
    pid = _read_pid()
    running = pid is not None and pid_alive(pid)
    return {
        "state": "running" if running else "stopped",
        "running": running,
        "pid": pid if running else None,
    }


def start_foreground(*, tray: bool = True) -> int:
    """Run the toaster in this process; write/clear the pid file around the loop."""
    snapshot = status_snapshot()
    if snapshot["running"]:
        print(f"toaster already running (pid={snapshot['pid']})", file=sys.stderr)
        return 1
    _write_pid(os.getpid())
    try:
        run_toaster(tray=tray)
    finally:
        _clear_pid()
    return 0


def start_background(*, tray: bool = True) -> int:
    """Spawn a detached pythonw process running ``dnd toaster start --fg``."""
    snapshot = status_snapshot()
    if snapshot["running"]:
        print(f"toaster already running (pid={snapshot['pid']})")
        return 0

    pythonw = Path(sys.executable).with_name("pythonw.exe")
    launcher = pythonw if pythonw.exists() else Path(sys.executable)

    args = [str(launcher), "-m", "dragndoc", "toaster", "start", "--fg"]
    if not tray:
        args.append("--no-tray")

    creationflags = 0
    if sys.platform == "win32":
        creationflags = (
            subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
            | subprocess.CREATE_NEW_PROCESS_GROUP
        )

    proc = subprocess.Popen(
        args,
        creationflags=creationflags,
        close_fds=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # wait for the child to write its pid; detect immediate failure.
    # 10s leaves headroom for cold pythonw.exe startup + dragndoc imports.
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if proc.poll() is not None and proc.returncode != 0:
            print(f"toaster failed to start (exit code {proc.returncode})", file=sys.stderr)
            return 1
        snapshot = status_snapshot()
        if snapshot["running"]:
            print(f"toaster started (pid={snapshot['pid']})")
            return 0
        time.sleep(0.1)
    print("toaster start requested, but it didn't report ready before the timeout", file=sys.stderr)
    return 1


def stop_toaster(*, timeout: float = 10.0) -> int:
    pid = _read_pid()
    if pid is None or not pid_alive(pid):
        _clear_pid()
        print("toaster: not running")
        return 0
    # The child runs as a detached pythonw.exe with no console, so
    # CTRL_BREAK can't reach it; terminate by handle instead.
    try:
        terminate_pid(pid)
    except OSError as exc:
        print(f"failed to signal toaster (pid={pid}): {exc}", file=sys.stderr)
        return 1
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            _clear_pid()
            print(f"toaster stopped (pid={pid})")
            return 0
        time.sleep(0.1)
    print(f"toaster did not stop within {timeout:.0f}s (pid={pid})", file=sys.stderr)
    return 1
