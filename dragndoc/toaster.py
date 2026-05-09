"""Poll the events table, fire Windows toasts, host a tray icon.

Standalone consumer process: invoke via ``dnd toaster``. Runs even
when the pipeline lives in a container, so toasts surface on the host.

Cursor file (``<data_dir>/toaster.cursor``) holds the last consumed
``events.id`` so restarts never miss or duplicate. The first run with a
non-existent cursor jumps to the current latest id (no replaying old
events).
"""

from __future__ import annotations

import logging
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
    running_label: Optional[str] = None  # "Digesting foo.pdf", "Scanning…", or None for idle
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

    def set_running(self, label: Optional[str]) -> None:
        with self._lock:
            self.running_label = label

    def toggle_notifications(self) -> bool:
        with self._lock:
            self.notifications_enabled = not self.notifications_enabled
            return self.notifications_enabled

    def is_enabled(self) -> bool:
        with self._lock:
            return self.notifications_enabled

    def status_text(self) -> str:
        with self._lock:
            if self.running_label:
                return _truncate(self.running_label, 80)
            if self.action_items > 0:
                noun = "file" if self.action_items == 1 else "files"
                return f"{self.action_items} {noun} ready for triage"
            if self.last_notification is not None:
                return _truncate(self.last_notification, 80)
            return "No notifications yet"


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _format_toast(event: dict[str, Any]) -> Optional[tuple[str, str]]:
    """Map an event row to ``(title, body)`` for a toast, or ``None`` to skip.

    Run-state events (``digest_started``, ``scan_*``) update the tray label
    only — they don't pop a notification. ``digest_finished`` fires a single
    "N files ready for triage" toast (only when the queue is non-empty).
    """
    kind = event.get("kind", "?")
    payload = event.get("payload") or {}
    if kind == "digest_finished":
        ready = int(payload.get("ready_count") or 0)
        failed = int(payload.get("failed") or 0)
        if ready > 0:
            noun = "file" if ready == 1 else "files"
            return "Drag'n'Doc", f"{ready} {noun} ready for triage"
        if failed > 0:
            f = payload.get("file") or "?"
            return "Error", f"failed to digest {f}"
        return None
    if kind == "scan_finished":
        return None  # silent
    if kind in {"digest_started", "scan_started"}:
        return None  # silent — these only adjust the tray status line
    if kind == "error":
        return "Error", f"{payload.get('file', '?')}: {payload.get('error', '?')}"
    if kind == "processed":  # legacy event from older pipelines
        body = f"{payload.get('file', '?')} → {payload.get('category', '?')}"
        target = payload.get("target")
        if target:
            body += f" ({target})"
        return "Drag'n'Doc", body
    return "Drag'n'Doc", f"{kind}: {payload}"


# title prefixes the toaster uses to flag severity. The Windows toast surface
# already labels the app, so we don't repeat "Drag'n'Doc" here
_ERROR_TITLES = {"Error"}
_WARNING_TITLES = {"Warning"}


def _level_for_title(title: str) -> int:
    if title in _ERROR_TITLES:
        return logging.ERROR
    if title in _WARNING_TITLES:
        return logging.WARNING
    return logging.INFO


def _apply_run_state(event: dict[str, Any], state: Optional[TrayState]) -> None:
    """Mirror digest_started/finished and scan_started/finished into the tray label."""
    if state is None:
        return
    kind = event.get("kind", "")
    payload = event.get("payload") or {}
    if kind == "digest_started":
        scope = payload.get("scope")
        if scope == "tree":
            count = payload.get("count")
            label = f"Digesting {count} files…" if count else "Digesting…"
        else:
            file = payload.get("file") or "…"
            label = f"Digesting {file}"
        state.set_running(label)
    elif kind == "scan_started":
        state.set_running("Scanning…")
    elif kind in {"digest_finished", "scan_finished"}:
        state.set_running(None)


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
        _apply_run_state(event, state)
        toast = _format_toast(event)
        if toast is not None:
            title, body = toast
            # mirror every user-facing notification into the log file so the
            # log is a complete record of what was surfaced — independent of
            # mute state and of whatever process emitted the source event.
            log.log(_level_for_title(title), "%s", body)
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
# tray menu actions
# ---------------------------------------------------------------------------


# singleton log-viewer subprocess, guarded by ``_log_lock`` so the tray's
# poll thread and "Log" callback can't race. Closed when the toaster exits
# so the viewer doesn't outlive its parent
_log_proc: Optional[subprocess.Popen] = None
_log_lock = threading.Lock()


def _spawn_log_viewer(log_file: Path) -> Optional[subprocess.Popen]:
    """Launch the Tk log viewer in its own pythonw process.

    Tk handles BiDi reordering correctly, which a TUI on top of Windows
    Terminal does not — Hebrew log lines surface in visual order here.
    """
    pyw = Path(sys.executable).with_name("pythonw.exe")
    interpreter = pyw if pyw.exists() else Path(sys.executable)
    try:
        return subprocess.Popen(
            [str(interpreter), "-m", "dragndoc.log_viewer", str(log_file)],
            cwd=str(REPO_ROOT),
        )
    except OSError as exc:
        log.warning("Could not launch log viewer: %s", exc)
        return None


def _open_log() -> None:
    """Open the log viewer; if one is already running, raise its window instead."""
    global _log_proc
    log_file = get_settings().logs_dir / LOG_FILENAME
    with _log_lock:
        if _log_proc is not None and _log_proc.poll() is None:
            _focus_pid(_log_proc.pid)
            return
        _log_proc = _spawn_log_viewer(log_file)


def _focus_pid(pid: int) -> bool:
    """Bring the first visible top-level window owned by ``pid`` to the front."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return False

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [EnumWindowsProc, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL

    SW_RESTORE = 9
    found: list[int] = []

    def _cb(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        win_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(win_pid))
        if win_pid.value == pid:
            found.append(hwnd)
            return False
        return True

    try:
        user32.EnumWindows(EnumWindowsProc(_cb), 0)
    except OSError as exc:
        log.debug("EnumWindows failed: %s", exc)
        return False
    if not found:
        return False
    hwnd = found[0]
    user32.ShowWindow(hwnd, SW_RESTORE)
    return bool(user32.SetForegroundWindow(hwnd))


def _close_log_viewer() -> None:
    """Terminate the log viewer subprocess, if any. Idempotent."""
    global _log_proc
    with _log_lock:
        proc = _log_proc
        _log_proc = None
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
    except OSError as exc:
        log.debug("log viewer terminate failed: %s", exc)
        return
    try:
        proc.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass


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


TRIAGE_WIN_COLS = 130
TRIAGE_WIN_ROWS = 34


def _ensure_dpi_aware() -> None:
    """Mark the process DPI-aware so screen-metric APIs return physical px.

    Without this, on a scaled display GetSystemMetrics returns virtualized
    (downscaled) values, but wt's ``--pos`` is interpreted in physical px —
    so a "centered" coordinate ends up near the top-left. Idempotent.
    """
    try:
        import ctypes

        try:
            # per_monitor_aware_v2 = -4 on Win10 1703+ gives best fidelity
            if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
                return
        except (AttributeError, OSError):
            pass
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR
            return
        except (AttributeError, OSError):
            pass
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass
    except Exception as exc:  # noqa: BLE001
        log.debug("DPI awareness setup failed: %s", exc)


def _system_dpi_scale() -> float:
    try:
        import ctypes

        dpi = ctypes.windll.user32.GetDpiForSystem()
        return (dpi / 96.0) if dpi else 1.0
    except (AttributeError, OSError):
        return 1.0


def _work_area_px() -> Optional[tuple[int, int, int, int]]:
    """Return (left, top, width, height) of the primary monitor's work area in physical px."""
    try:
        import ctypes
        from ctypes import wintypes

        rect = wintypes.RECT()
        SPI_GETWORKAREA = 0x0030
        if not ctypes.windll.user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
            return None
        return (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)
    except Exception as exc:  # noqa: BLE001
        log.debug("could not query work area: %s", exc)
        return None


def _launch_triage() -> None:
    """Open a console window running triage.cmd, centered on screen. Prefers Windows Terminal."""
    if not TRIAGE_SCRIPT.exists():
        log.warning("Triage script not found: %s", TRIAGE_SCRIPT)
        return
    _ensure_dpi_aware()
    wt = shutil.which("wt.exe")
    mintty = _find_mintty()
    if wt:
        scale = _system_dpi_scale()
        # default Cascadia Mono ~10×20 px per cell at 96 DPI; chrome ~30/100 px
        # wt scales font with DPI, so the actual window size scales too
        est_w = int((TRIAGE_WIN_COLS * 10 + 30) * scale)
        est_h = int((TRIAGE_WIN_ROWS * 20 + 100) * scale)
        cmd = [wt, "-w", "new"]
        wa = _work_area_px()
        if wa is not None:
            wa_l, wa_t, wa_w, wa_h = wa
            x = wa_l + max(0, (wa_w - est_w) // 2)
            y = wa_t + max(0, (wa_h - est_h) // 2)
            cmd += ["--pos", f"{x},{y}"]
            log.debug("triage center: work=%s window~=%dx%d → pos=%d,%d", wa, est_w, est_h, x, y)
        cmd += ["--size", f"{TRIAGE_WIN_COLS},{TRIAGE_WIN_ROWS}"]
        cmd += ["-d", str(REPO_ROOT), "cmd.exe", "/c", str(TRIAGE_SCRIPT)]
    elif mintty:
        script = str(TRIAGE_SCRIPT).replace("\\", "/")
        cmd = [mintty, "-h", "error", "-p", "center", "--", "cmd.exe", "/c", script]
    else:
        cmd = ["conhost.exe", "cmd.exe", "/c", str(TRIAGE_SCRIPT)]
    try:
        subprocess.Popen(cmd, cwd=str(REPO_ROOT))
    except OSError as exc:
        log.warning("Could not launch triage: %s", exc)


def _count_ready_for_triage() -> int:
    """Number of inbox files queued for triage (filled by digest, drained by /triage)."""
    try:
        from dragndoc.triage import count as q_count

        return q_count(inbox_only=True)
    except Exception as exc:  # noqa: BLE001
        log.debug("triage count failed: %s", exc)
        return 0


# ---------------------------------------------------------------------------
# tray icon
# ---------------------------------------------------------------------------


def _make_icon_image(red_dot: bool = False):
    """Generate a 64×64 RGBA icon: a stylized document with a folded corner
    sitting on a blue rounded-square tile. ``red_dot`` overlays a badge in
    the upper-right corner to indicate unhandled events."""
    from PIL import Image, ImageDraw

    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    blue = (37, 99, 235, 255)
    paper = (255, 255, 255, 255)
    fold_shadow = (210, 220, 240, 255)
    text_line = (37, 99, 235, 200)

    draw.rounded_rectangle((2, 2, size - 2, size - 2), radius=12, fill=blue)

    # downward "drop" arrow above the document, in white on the blue tile
    arrow_cx = 32
    shaft_w = 6
    draw.rectangle((arrow_cx - shaft_w // 2, 6, arrow_cx + shaft_w // 2, 20), fill=paper)
    draw.polygon([(arrow_cx - 9, 18), (arrow_cx + 9, 18), (arrow_cx, 28)], fill=paper)

    # document body, sitting below the arrow with a folded upper-right corner
    doc_l, doc_t, doc_r, doc_b = 12, 30, 52, 58
    fold = 9
    body = [
        (doc_l, doc_t),
        (doc_r - fold, doc_t),
        (doc_r, doc_t + fold),
        (doc_r, doc_b),
        (doc_l, doc_b),
    ]
    draw.polygon(body, fill=paper)
    draw.polygon(
        [(doc_r - fold, doc_t), (doc_r, doc_t + fold), (doc_r - fold, doc_t + fold)],
        fill=fold_shadow,
    )

    # text lines on the page
    line_h = 3
    for i, width_ratio in enumerate((0.8, 0.6)):
        y = doc_t + 9 + i * 8
        x2 = doc_l + 4 + int((doc_r - doc_l - 8) * width_ratio)
        draw.rectangle((doc_l + 4, y, x2, y + line_h), fill=text_line)

    if red_dot:
        cx, cy, r = 52, 14, 11
        draw.ellipse((cx - r - 1, cy - r - 1, cx + r + 1, cy + r + 1), fill=paper)
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
                    # persist only after successful consumption so restarts do not skip events
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
    print("[dragndoc] Toaster polling events table; right-click the tray icon to exit.")
    try:
        icon.run()
    finally:
        stop_event.set()
        poll_thread.join(timeout=2.0)
        _close_log_viewer()
        notifier.close()


def _run_headless(poll_interval: float) -> None:
    """Same loop as ``_run_with_tray`` but without the tray. Used for ``--no-tray``."""
    settings = get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    cursor_file = cursor_path()
    cursor = _initial_cursor(cursor_file)
    notifier = Notifier()

    log.info("Toaster polling events table (cursor=%d, headless)", cursor.last_id)
    print("[dragndoc] Toaster polling events table; Ctrl-C to stop.")
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
# lifecycle: PID-file based start / stop / status
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
    # wait for the child to write its pid; detect immediate failure
    # 10s leaves headroom for cold pythonw.exe startup + dragndoc imports
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


def stop_toaster(*, timeout: float = 10.0, quiet: bool = False) -> int:
    pid = _read_pid()
    if pid is None or not pid_alive(pid):
        _clear_pid()
        if not quiet:
            print("toaster: not running")
        return 0
    # the child runs as a detached pythonw.exe with no console, so
    # ctrl_break can't reach it; terminate by handle instead
    try:
        terminate_pid(pid)
    except OSError as exc:
        print(f"failed to signal toaster (pid={pid}): {exc}", file=sys.stderr)
        return 1
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            _clear_pid()
            if not quiet:
                print(f"toaster stopped (pid={pid})")
            return 0
        time.sleep(0.1)
    print(f"toaster did not stop within {timeout:.0f}s (pid={pid})", file=sys.stderr)
    return 1


def restart_toaster(*, tray: bool = True, timeout: float = 10.0) -> int:
    """Stop a running toaster (if any) and start a fresh background one."""
    rc = stop_toaster(timeout=timeout, quiet=True)
    if rc != 0:
        return rc
    return start_background(tray=tray)
