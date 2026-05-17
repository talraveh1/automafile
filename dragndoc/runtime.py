"""Container runtime supervisor for the watcher process."""

from __future__ import annotations

import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from dragndoc.config import get_settings
from dragndoc.log import get_logger
from dragndoc.process import pid_alive


log = get_logger(__name__)

_POLL_INTERVAL = 0.5


class WatcherProcess(Protocol):
    pid: int
    returncode: int | None

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


@dataclass(frozen=True)
class RuntimePaths:
    state_dir: Path
    disabled_file: Path
    pid_file: Path
    heartbeat_file: Path


def runtime_paths() -> RuntimePaths:
    state_dir = get_settings().data_dir / "runtime"
    return RuntimePaths(
        state_dir=state_dir,
        disabled_file=state_dir / "watch.disabled",
        pid_file=state_dir / "watch.pid",
        heartbeat_file=state_dir / "watch.heartbeat",
    )


# the watcher refreshes ``watch.heartbeat`` each tick; we treat anything
# fresher than this as "running". A generous multiple of the 1 s tick
# tolerates short pauses (e.g. a Docker desktop suspend) without flapping.
HEARTBEAT_STALE_AFTER = 15.0


def write_heartbeat(paths: RuntimePaths | None = None) -> None:
    """Bump ``watch.heartbeat``'s mtime so cross-namespace observers can
    confirm liveness without resolving a foreign PID."""
    paths = paths or runtime_paths()
    try:
        _ensure_state_dir(paths)
        paths.heartbeat_file.touch(exist_ok=True)
        # touch() updates mtime even when the file already exists
    except OSError:
        pass


def _heartbeat_fresh(paths: RuntimePaths) -> bool:
    try:
        mtime = paths.heartbeat_file.stat().st_mtime
    except OSError:
        return False
    return (time.time() - mtime) <= HEARTBEAT_STALE_AFTER


def _ensure_state_dir(paths: RuntimePaths) -> None:
    paths.state_dir.mkdir(parents=True, exist_ok=True)


def request_stop(paths: RuntimePaths | None = None) -> None:
    paths = paths or runtime_paths()
    _ensure_state_dir(paths)
    paths.disabled_file.write_text("stopped\n", encoding="utf-8")


def request_start(paths: RuntimePaths | None = None) -> None:
    paths = paths or runtime_paths()
    _ensure_state_dir(paths)
    paths.disabled_file.unlink(missing_ok=True)


def _write_pid(paths: RuntimePaths, pid: int) -> None:
    _ensure_state_dir(paths)
    paths.pid_file.write_text(f"{pid}\n", encoding="utf-8")


def _read_pid(paths: RuntimePaths) -> int | None:
    try:
        raw = paths.pid_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        # ignore stale or partially written pid files instead of crashing status checks
        return int(raw)
    except ValueError:
        return None


def _clear_pid(paths: RuntimePaths) -> None:
    paths.pid_file.unlink(missing_ok=True)


def status_snapshot(paths: RuntimePaths | None = None) -> dict[str, object]:
    paths = paths or runtime_paths()
    disabled = paths.disabled_file.exists()
    pid = _read_pid(paths)
    # liveness is decided by heartbeat freshness — pid_alive doesn't cross
    # the container's PID namespace, so the host's toaster can't trust it.
    # we still fall back to pid_alive on the off-chance the heartbeat file
    # is missing (e.g. running an older watcher build for a moment).
    running = _heartbeat_fresh(paths) or (pid is not None and pid_alive(pid))
    if running:
        state = "running"
    elif disabled:
        state = "stopped"
    else:
        state = "idle"
    return {
        "state": state,
        "disabled": disabled,
        "running": running,
        "pid": pid if running else None,
    }


def wait_for_running(
    running: bool,
    *,
    timeout: float = 10.0,
    poll_interval: float = 0.1,
    paths: RuntimePaths | None = None,
) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        snapshot = status_snapshot(paths)
        if bool(snapshot["running"]) is running:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll_interval)


def _spawn_watcher() -> WatcherProcess:
    settings = get_settings()
    return subprocess.Popen(
        [sys.executable, "-c", "from dragndoc.watcher import run_watcher; run_watcher()"],
        cwd=str(settings.repo_root),
    )


def _run_supervisor_loop(
    *,
    paths: RuntimePaths,
    spawn_watcher: Callable[[], WatcherProcess],
    poll_interval: float,
    sleep_fn: Callable[[float], None],
    should_exit: Callable[[], bool] | None = None,
    loop_limit: int | None = None,
) -> int:
    _ensure_state_dir(paths)
    child: WatcherProcess | None = None
    intentional_stop = False
    loops = 0

    while True:
        loops += 1
        if loop_limit is not None and loops > loop_limit:
            return 0

        if should_exit is not None and should_exit():
            # drain the child first so shutdown clears the pid file only after the watcher is gone
            if child is not None and child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=poll_interval)
                except subprocess.TimeoutExpired:
                    sleep_fn(poll_interval)
                    continue
            _clear_pid(paths)
            return 0

        disabled = paths.disabled_file.exists()

        if child is not None:
            exit_code = child.poll()
            if exit_code is not None:
                _clear_pid(paths)
                child = None
                # report crashes to the caller, but keep quiet for requested stops and disabled mode
                if disabled or intentional_stop:
                    intentional_stop = False
                    log.info("Supervisor: watcher stopped intentionally")
                else:
                    log.error("Supervisor: watcher exited unexpectedly with code %s", exit_code)
                    return exit_code or 1
                sleep_fn(poll_interval)
                continue

        if disabled:
            if child is not None:
                intentional_stop = True
                # keep honoring the stop marker until the child has actually exited
                log.info("Supervisor: stopping watcher on request")
                child.terminate()
                try:
                    child.wait(timeout=poll_interval)
                except subprocess.TimeoutExpired:
                    sleep_fn(poll_interval)
                    continue
                continue
            sleep_fn(poll_interval)
            continue

        if child is None:
            # only spawn when the stop marker is absent and no watcher is already running
            child = spawn_watcher()
            _write_pid(paths, child.pid)
            log.info("Supervisor: started watcher pid=%s", child.pid)

        sleep_fn(poll_interval)


def supervise() -> int:
    paths = runtime_paths()
    request_start(paths)

    child_ref: dict[str, WatcherProcess | None] = {"proc": None}
    shutting_down = False

    def spawn_watcher() -> WatcherProcess:
        proc = _spawn_watcher()
        child_ref["proc"] = proc
        return proc

    def _handle_shutdown(signum, _frame) -> None:  # noqa: ANN001
        nonlocal shutting_down
        shutting_down = True
        log.info("Supervisor: received signal %s", signum)
        request_stop(paths)
        proc = child_ref["proc"]
        if proc is not None and proc.poll() is None:
            proc.terminate()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _handle_shutdown)

    try:
        return _run_supervisor_loop(
            paths=paths,
            spawn_watcher=spawn_watcher,
            poll_interval=_POLL_INTERVAL,
            sleep_fn=time.sleep,
            should_exit=lambda: shutting_down,
        )
    finally:
        _clear_pid(paths)
        if shutting_down:
            request_start(paths)
