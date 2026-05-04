"""Tiny Tk window that displays the rolling dragndoc.log file.

Spawned as a subprocess from the toaster's tray "Log" menu so each click
gets its own Tk root in its own process — no threading conflicts with
the pystray main thread.

    python -m dragndoc.log_viewer [path/to/log]

Defaults to ``<data_dir>/logs/dragndoc.log`` if no path is given.
"""

from __future__ import annotations

import sys
from pathlib import Path


TAIL_LINES = 500
REFRESH_INTERVAL_MS = 1000


def _resolve_log_path(argv: list[str]) -> Path:
    if len(argv) > 1:
        return Path(argv[1])
    from dragndoc.config import get_settings
    return get_settings().logs_dir / "dragndoc.log"


def _read_tail(path: Path, max_lines: int) -> str:
    if not path.exists():
        return f"(log file not found: {path})\n"
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError as exc:
        return f"(could not read log: {exc})\n"
    return "".join(lines[-max_lines:])


def main(argv: list[str]) -> int:
    import tkinter as tk
    from tkinter import ttk

    log_path = _resolve_log_path(argv)

    root = tk.Tk()
    root.title(f"Drag'n'Doc log — {log_path.name}")
    root.geometry("960x540")

    auto_refresh = tk.BooleanVar(value=True)

    toolbar = ttk.Frame(root, padding=(8, 6))
    toolbar.pack(side=tk.TOP, fill=tk.X)

    path_label = ttk.Label(toolbar, text=str(log_path), foreground="#475569")
    path_label.pack(side=tk.LEFT)

    ttk.Checkbutton(toolbar, text="Auto-refresh", variable=auto_refresh).pack(side=tk.RIGHT)
    refresh_btn = ttk.Button(toolbar, text="Refresh now")
    refresh_btn.pack(side=tk.RIGHT, padx=(0, 8))

    text_frame = ttk.Frame(root)
    text_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

    yscroll = ttk.Scrollbar(text_frame, orient=tk.VERTICAL)
    yscroll.pack(side=tk.RIGHT, fill=tk.Y)

    text = tk.Text(
        text_frame,
        wrap=tk.NONE,
        font=("Consolas", 10),
        bg="#0f172a", fg="#e2e8f0", insertbackground="#e2e8f0",
        yscrollcommand=yscroll.set,
        state=tk.DISABLED,
    )
    text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    yscroll.config(command=text.yview)

    def refresh() -> None:
        content = _read_tail(log_path, TAIL_LINES)
        text.config(state=tk.NORMAL)
        text.delete("1.0", tk.END)
        text.insert(tk.END, content)
        text.config(state=tk.DISABLED)
        text.see(tk.END)

    refresh_btn.config(command=refresh)
    refresh()

    def tick() -> None:
        if auto_refresh.get():
            refresh()
        root.after(REFRESH_INTERVAL_MS, tick)

    root.after(REFRESH_INTERVAL_MS, tick)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
