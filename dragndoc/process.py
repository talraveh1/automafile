"""Cross-platform pid liveness and termination helpers.

``os.kill(pid, 0)`` is a POSIX idiom for probing whether a pid is alive,
but on Windows Python's ``os.kill`` maps to
``OpenProcess(PROCESS_ALL_ACCESS) + TerminateProcess(handle, sig)`` —
so it either fails with ACCESS_DENIED (false negative for liveness) or
actually terminates the target with exit code 0. These helpers use the
right Win32 calls (``PROCESS_QUERY_LIMITED_INFORMATION`` for liveness,
``PROCESS_TERMINATE`` for termination) so a parent shell can sanely
manage a detached ``pythonw.exe`` child.
"""

from __future__ import annotations

import os
import signal
import sys


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _win_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def terminate_pid(pid: int) -> None:
    """Best-effort terminate. Raises ``OSError`` if the process can't be
    opened or terminated."""
    if sys.platform == "win32":
        _win_terminate(pid)
        return
    os.kill(pid, signal.SIGTERM)


# --- Windows ---------------------------------------------------------------

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_TERMINATE = 0x0001
_STILL_ACTIVE = 259


def _win_kernel32():
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k32.GetExitCodeProcess.restype = wintypes.BOOL
    k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k32.TerminateProcess.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL
    return k32


def _win_pid_alive(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    k32 = _win_kernel32()
    handle = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        exit_code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == _STILL_ACTIVE
    finally:
        k32.CloseHandle(handle)


def _win_terminate(pid: int) -> None:
    import ctypes

    k32 = _win_kernel32()
    handle = k32.OpenProcess(_PROCESS_TERMINATE, False, pid)
    if not handle:
        err = ctypes.get_last_error()
        raise OSError(err, f"OpenProcess(pid={pid}) failed")
    try:
        if not k32.TerminateProcess(handle, 0):
            err = ctypes.get_last_error()
            raise OSError(err, f"TerminateProcess(pid={pid}) failed")
    finally:
        k32.CloseHandle(handle)
