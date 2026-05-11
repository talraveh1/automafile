"""Forwarder to the squigglelog package with dragndoc defaults.

Spawned as a subprocess from the toaster's tray "Log" menu so each click
gets its own QApplication in its own process — no threading conflict
with the pystray main thread.

    python -m dragndoc.log_viewer [path/to/log]

Defaults to ``<data_dir>/logs/dragndoc.log`` if no path is given.
"""

from __future__ import annotations

import sys
from pathlib import Path

from squigglelog import PRESETS, run

from dragndoc.config import get_settings


def main(argv: list[str] | None = None) -> int:
    args = sys.argv if argv is None else argv
    if len(args) > 1:
        path = Path(args[1])
    else:
        path = get_settings().logs_dir / "dragndoc.log"
    return run(
        path=path,
        line_re=PRESETS["loguru-default"],
        title="Drag'n'Doc Log",
        app_name="Drag'n'Doc Log Viewer",
    )


if __name__ == "__main__":
    raise SystemExit(main())
