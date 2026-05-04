"""Auto-start the Drag'n'Doc toaster at user logon (no admin required).

What this does, idempotently:

1. Drops a shortcut into the user's Startup folder
   (``%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\Startup``) that
   launches the venv's ``pythonw.exe`` with ``-m dragndoc toaster start --fg``.
2. Registers ``DragnDoc.Toaster`` as a per-user AUMID under
   ``HKCU:\\Software\\Classes\\AppUserModelId\\``. Without this, Windows
   silently drops every toast — windows-toasts succeeds at sending but the
   notification center never sees it because the AUMID isn't recognised.

Why not Task Scheduler? ``schtasks /Create /SC ONLOGON`` requires elevation;
for a personal-project per-user daemon, the Startup folder is simpler.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
VENV = REPO / ".venv"
SHORTCUT_NAME = "DragnDoc Toaster.lnk"

# AUMID = Application User Model ID. Must be a single-segment dotted name,
# 1–129 chars. Windows looks this up under HKCU\Software\Classes\AppUserModelId
# to attribute toast notifications. Keep this in sync with notifier.AUMID.
AUMID = "DragnDoc.Toaster"
AUMID_DISPLAY_NAME = "Drag'n'Doc"
AUMID_REG_PATH = rf"Software\Classes\AppUserModelId\{AUMID}"


def _venv_pythonw(venv: Path) -> Path:
    return venv / "Scripts" / "pythonw.exe"


def _startup_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise RuntimeError("APPDATA not set; cannot locate the Startup folder.")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def _shortcut_path() -> Path:
    return _startup_dir() / SHORTCUT_NAME


def _ps_quote(s: str) -> str:
    return s.replace("'", "''")


def _install_shortcut() -> int:
    pythonw = _venv_pythonw(VENV)
    if not pythonw.exists():
        print(f"venv pythonw.exe not found at {pythonw}\nRun scripts\\install.py first.", file=sys.stderr)
        return 1

    target = _shortcut_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    # WScript.Shell is the standard COM interface for creating .lnk files.
    # Driving it from PowerShell avoids a pywin32 dependency.
    script = (
        "$ws = New-Object -ComObject WScript.Shell;"
        f"$lnk = $ws.CreateShortcut('{_ps_quote(str(target))}');"
        f"$lnk.TargetPath = '{_ps_quote(str(pythonw))}';"
        "$lnk.Arguments = '-m dragndoc toaster start --fg';"
        f"$lnk.WorkingDirectory = '{_ps_quote(str(REPO))}';"
        "$lnk.WindowStyle = 7;"
        "$lnk.Description = 'Tail DragnDoc events table and fire Windows toasts.';"
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
    print(f"Installed shortcut: {target}")
    return 0


def _register_aumid() -> None:
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, AUMID_REG_PATH) as key:
        winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, AUMID_DISPLAY_NAME)
    print(f"Registered AUMID: {AUMID} (DisplayName={AUMID_DISPLAY_NAME!r})")


def _unregister_aumid() -> None:
    import winreg
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, AUMID_REG_PATH)
        print(f"Unregistered AUMID: {AUMID}")
    except FileNotFoundError:
        print(f"AUMID not registered: {AUMID}")


def _aumid_registered() -> bool:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUMID_REG_PATH):
            return True
    except FileNotFoundError:
        return False


def install() -> int:
    rc = _install_shortcut()
    if rc != 0:
        return rc
    _register_aumid()
    print()
    print("It will auto-start at the next user logon.")
    print("To start it now: dnd toaster start")
    return 0


def uninstall() -> int:
    target = _shortcut_path()
    if target.exists():
        target.unlink()
        print(f"Removed shortcut: {target}")
    else:
        print(f"Shortcut not installed: {target}")
    _unregister_aumid()
    return 0


def status() -> int:
    target = _shortcut_path()
    shortcut_ok = target.exists()
    aumid_ok = _aumid_registered()
    print(f"Shortcut: {'OK    ' if shortcut_ok else 'MISSING'} {target}")
    print(f"AUMID:    {'OK    ' if aumid_ok else 'MISSING'} HKCU\\{AUMID_REG_PATH}")
    return 0 if (shortcut_ok and aumid_ok) else 1
