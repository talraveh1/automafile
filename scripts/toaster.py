"""Auto-start the Automafile toaster at user logon (no admin required).

Drops a shortcut into the user's Startup folder
(``%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\Startup``) that
launches the venv's ``pythonw.exe`` with ``-m automafile toaster``. No
console window flashes, no UAC prompt, no Task Scheduler entry.

    python scripts\\toaster.py            # install / refresh
    python scripts\\toaster.py --status   # show whether the shortcut is present
    python scripts\\toaster.py --uninstall

Idempotent: re-running ``install`` overwrites the existing shortcut.

Why not Task Scheduler? ``schtasks /Create /SC ONLOGON`` requires
elevation; for a personal-project per-user daemon, the Startup folder is
the simpler, friction-free idiom.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
VENV = REPO / ".venv"
SHORTCUT_NAME = "Automafile Toaster.lnk"


def venv_pythonw(venv: Path) -> Path:
    return venv / "Scripts" / "pythonw.exe"


def startup_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        sys.exit("APPDATA not set; cannot locate the Startup folder.")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def shortcut_path() -> Path:
    return startup_dir() / SHORTCUT_NAME


def _ps_quote(s: str) -> str:
    """Single-quote a string for safe interpolation into a PowerShell here-string literal."""
    return s.replace("'", "''")


def install() -> int:
    pythonw = venv_pythonw(VENV)
    if not pythonw.exists():
        sys.exit(f"venv pythonw.exe not found at {pythonw}\nRun scripts\\install.py first.")

    target = shortcut_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    # WScript.Shell is the standard COM interface for creating .lnk files.
    # Driving it from PowerShell avoids a pywin32 dependency.
    script = (
        "$ws = New-Object -ComObject WScript.Shell;"
        f"$lnk = $ws.CreateShortcut('{_ps_quote(str(target))}');"
        f"$lnk.TargetPath = '{_ps_quote(str(pythonw))}';"
        "$lnk.Arguments = '-m automafile toaster';"
        f"$lnk.WorkingDirectory = '{_ps_quote(str(REPO))}';"
        "$lnk.WindowStyle = 7;"  # 7 = minimized; pythonw has no window anyway, belt-and-braces
        "$lnk.Description = 'Tail Automafile events.jsonl and fire Windows toasts.';"
        "$lnk.Save()"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(result.stdout, end="")
        print(result.stderr, end="", file=sys.stderr)
        return result.returncode

    print(f"Installed: {target}")
    print(f"  target  : {pythonw} -m automafile toaster")
    print(f"  workdir : {REPO}")
    print()
    print("It will auto-start at the next user logon.")
    print(f"To start it now: Start-Process '{pythonw}' -ArgumentList '-m','automafile','toaster' -WindowStyle Hidden")
    return 0


def uninstall() -> int:
    target = shortcut_path()
    if not target.exists():
        print(f"Not installed: {target}")
        return 0
    target.unlink()
    print(f"Removed: {target}")
    return 0


def status() -> int:
    target = shortcut_path()
    if target.exists():
        print(f"Installed: {target}")
        return 0
    print(f"Not installed: {target}")
    return 1


def main() -> int:
    if sys.platform != "win32":
        sys.exit("This script is Windows-only.")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uninstall", action="store_true", help="Remove the Startup shortcut.")
    parser.add_argument("--status", action="store_true", help="Check whether the shortcut is present.")
    args = parser.parse_args()

    if args.uninstall:
        return uninstall()
    if args.status:
        return status()
    return install()


if __name__ == "__main__":
    raise SystemExit(main())
