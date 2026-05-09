"""Allows ``python -m dragndoc`` invocation."""

import sys

from dragndoc.cli import app


if __name__ == "__main__":
    # hebrew/RTL filenames are common in this project's docs tree; the default
    # cp1252 console encoding on Windows can't render them. Reconfigure stdio
    # to UTF-8 so listings and JSON output don't crash.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            pass
    app()
