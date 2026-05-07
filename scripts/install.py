"""Bootstrap Drag'n'Doc: create a venv, install editable, run dnd bootstrap.

Run with the system Python 3.12+:

    python scripts\\install.py

The script is idempotent — re-running it upgrades pip and reinstalls deps
without recreating the venv.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Sequence


REPO = Path(__file__).resolve().parent.parent
VENV = REPO / ".venv"


def venv_python(venv: Path) -> Path:
    if sys.platform == "win32":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def venv_dnd(venv: Path) -> Path:
    if sys.platform == "win32":
        return venv / "Scripts" / "dnd.exe"
    return venv / "bin" / "dnd"


def venv_site_packages(venv: Path) -> Path:
    if sys.platform == "win32":
        return venv / "Lib" / "site-packages"
    # Resolve the lib/python3.X/site-packages path without globbing.
    lib = venv / "lib"
    candidates = sorted(lib.glob("python3.*/site-packages"))
    if not candidates:
        raise RuntimeError(f"no site-packages under {lib}")
    return candidates[-1]


def write_pycache_pth(venv: Path, repo: Path) -> None:
    """Drop a .pth file that redirects bytecode caches to <repo>/build/pycache.

    Lines starting with ``import`` in a .pth file are executed by site.py at
    Python startup, before any package's ``__init__.py`` (or any conftest.py)
    runs — so this catches even the loader's own .pyc writes that an in-code
    ``sys.pycache_prefix`` setting cannot.
    """
    target = venv_site_packages(venv) / "_dragndoc_pycache.pth"
    prefix = (repo / "build" / "pycache").resolve().as_posix()
    line = f"import sys; sys.pycache_prefix = {prefix!r}\n"
    target.write_text(line, encoding="utf-8")
    print(f">> wrote {target}")


def run(cmd: Sequence[str | Path], **kwargs) -> None:
    rendered = [str(c) for c in cmd]
    print(f">> {' '.join(rendered)}")
    subprocess.check_call(rendered, **kwargs)


def main() -> int:
    if sys.version_info < (3, 12):
        sys.exit(f"Python 3.12+ required (got {sys.version.split()[0]}).")

    py = venv_python(VENV)
    dnd = venv_dnd(VENV)
    if not py.exists():
        print(f"Creating venv at {VENV}")
        run([sys.executable, "-m", "venv", str(VENV)])

    run([py, "-m", "pip", "install", "--upgrade", "pip"])
    run([py, "-m", "pip", "install", "-e", ".[dev]"], cwd=REPO)
    write_pycache_pth(VENV, REPO)
    run([dnd, "bootstrap"], cwd=REPO)

    print()
    print("Bootstrap complete. Next steps:")
    print(f"  1. Edit {REPO / 'config.jsonc'} if your defaults differ.")
    print("  2. Pin <docs>\\<inbox> to 'Always keep on this device' in OneDrive.")
    print(f"  3. Start the watcher: {dnd} watch start --fg")
    print(f"  4. Start the toaster: {dnd} toaster start")
    print(f"     (or '{dnd} toaster install' to auto-start it at logon)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
