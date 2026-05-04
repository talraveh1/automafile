"""Tests for the container watcher supervisor."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from dragndoc.cli import app
from dragndoc.runtime import RuntimePaths, _run_supervisor_loop, runtime_paths, status_snapshot


runner = CliRunner()


class _FakeProcess:
    def __init__(self, pid: int, polls: list[int | None]) -> None:
        self.pid = pid
        self._polls = list(polls)
        self.returncode: int | None = None
        self.terminate_called = False

    def poll(self) -> int | None:
        if self.returncode is not None:
            return self.returncode
        if not self._polls:
            return None
        value = self._polls.pop(0)
        if value is not None:
            self.returncode = value
        return value

    def terminate(self) -> None:
        self.terminate_called = True
        self.returncode = 0

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode or 0


def _paths(base: Path) -> RuntimePaths:
    return RuntimePaths(
        state_dir=base,
        disabled_file=base / "watch.disabled",
        pid_file=base / "watch.pid",
    )


def test_watch_without_subcommand_shows_help():
    res = runner.invoke(app, ["watch"])
    assert res.exit_code == 0, res.output
    assert "Usage:" in res.output
    assert "start" in res.output
    assert "stop" in res.output
    assert "status" in res.output


def test_top_level_help_supports_dash_h():
    res = runner.invoke(app, ["-h"])
    assert res.exit_code == 0, res.output
    assert "Usage:" in res.output


def test_watch_group_help_supports_dash_h():
    res = runner.invoke(app, ["watch", "-h"])
    assert res.exit_code == 0, res.output
    assert "Control the watcher." in res.output


def test_watch_start_help_supports_dash_h():
    res = runner.invoke(app, ["watch", "start", "-h"])
    assert res.exit_code == 0, res.output
    assert "--fg" in res.output


def test_process_help_supports_dash_h():
    res = runner.invoke(app, ["process", "-h"])
    assert res.exit_code == 0, res.output
    assert "--dry-run" in res.output


def test_watch_start_fg_runs_foreground_when_not_supervised():
    with patch("dragndoc.watcher.run_watcher") as mock_run:
        res = runner.invoke(app, ["watch", "start", "--fg"])
    assert res.exit_code == 0, res.output
    mock_run.assert_called_once()


def test_watch_control_commands_toggle_disabled_flag(tmp_path):
    paths = runtime_paths()

    res_stop = runner.invoke(app, ["watch", "stop"])
    assert res_stop.exit_code == 0, res_stop.output
    snap = status_snapshot(paths)
    assert snap["state"] == "stopped"
    assert snap["disabled"] is True

    res_start = runner.invoke(app, ["watch", "start", "--no-wait"])
    assert res_start.exit_code == 0, res_start.output
    snap = status_snapshot(paths)
    assert snap["state"] == "idle"
    assert snap["disabled"] is False


def test_supervisor_exits_when_watcher_dies_unexpectedly(tmp_path):
    paths = _paths(tmp_path / "runtime")
    proc = _FakeProcess(pid=1234, polls=[None, 7])

    code = _run_supervisor_loop(
        paths=paths,
        spawn_watcher=lambda: proc,
        poll_interval=0.0,
        sleep_fn=lambda _seconds: None,
        loop_limit=5,
    )

    assert code == 7
    assert not paths.pid_file.exists()


def test_supervisor_stops_watcher_and_stays_alive_when_disabled(tmp_path):
    paths = _paths(tmp_path / "runtime")
    proc = _FakeProcess(pid=4321, polls=[None, None, None])
    sleeps = {"count": 0}

    def sleep_then_disable(_seconds: float) -> None:
        sleeps["count"] += 1
        if sleeps["count"] == 1:
            paths.state_dir.mkdir(parents=True, exist_ok=True)
            paths.disabled_file.write_text("stopped\n", encoding="utf-8")

    code = _run_supervisor_loop(
        paths=paths,
        spawn_watcher=lambda: proc,
        poll_interval=0.0,
        sleep_fn=sleep_then_disable,
        loop_limit=6,
    )

    assert code == 0
    assert proc.terminate_called is True
    snap = status_snapshot(paths)
    assert snap["state"] == "stopped"
    assert snap["running"] is False


def test_supervisor_exits_cleanly_when_shutdown_requested(tmp_path):
    paths = _paths(tmp_path / "runtime")
    proc = _FakeProcess(pid=9999, polls=[None, None, None])
    state = {"stop": False, "sleeps": 0}

    def sleep_then_stop(_seconds: float) -> None:
        state["sleeps"] += 1
        if state["sleeps"] == 1:
            state["stop"] = True

    code = _run_supervisor_loop(
        paths=paths,
        spawn_watcher=lambda: proc,
        poll_interval=0.0,
        sleep_fn=sleep_then_stop,
        should_exit=lambda: state["stop"],
        loop_limit=5,
    )

    assert code == 0
    assert proc.terminate_called is True
    assert not paths.pid_file.exists()
