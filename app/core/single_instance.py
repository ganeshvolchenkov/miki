"""Keep Miki to one running copy.

Launching Miki repeatedly used to leave several full copies alive (each with its own WebView2
browser tree of ~500 MB). A named Windows mutex lets a second launch hand focus to the first
window and exit instead.
"""

from __future__ import annotations

import os
import sys

_MUTEX_NAME = "Local\\MikiAssistantSingleInstance"
_ERROR_ALREADY_EXISTS = 183
_WINDOW_TITLE = "Miki Command Center"

_handle = None  # keep the mutex alive for the life of the process
_named_handles: dict[str, int] = {}


def acquire() -> bool:
    """True if this process is (now) the only Miki. False if another copy already runs."""
    global _handle
    if sys.platform != "win32" or os.getenv("MIKI_ALLOW_MULTIPLE"):
        return True

    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, False, _MUTEX_NAME)
    if not handle:
        return True  # can't tell; never block startup
    if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        return False
    _handle = handle
    return True


def acquire_named(name: str) -> bool:
    """Claim a machine-wide lock called ``name`` (e.g. "the phone bot"). False if another process holds it."""
    if sys.platform != "win32" or os.getenv("MIKI_ALLOW_MULTIPLE"):
        return True
    if name in _named_handles:
        return True  # this process already holds it

    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, False, "Local" + chr(92) + name)  # Local\<name>
    if not handle:
        return True  # can't tell; never block startup
    if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        return False
    _named_handles[name] = handle
    return True


def focus_existing_window() -> bool:
    """Bring the already-running Miki window to the front."""
    if sys.platform != "win32":
        return False

    import ctypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    hwnd = user32.FindWindowW(None, _WINDOW_TITLE)
    if not hwnd:
        return False
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    user32.SetForegroundWindow(hwnd)
    return True
