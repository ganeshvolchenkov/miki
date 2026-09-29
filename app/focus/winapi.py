"""The only place that talks to Windows: monitors, top-level windows, who is in front.

Everything else in ``app.focus`` sees plain dataclasses, so the guard and the service can be tested with a fake
``Desktop``. Coordinates are physical pixels (per-monitor DPI aware) so a rectangle means the same thing to
Windows, to Chrome and to the pet.
"""

from __future__ import annotations

import logging
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

logger = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    w: int
    h: int

    @property
    def right(self) -> int:
        return self.x + self.w

    @property
    def bottom(self) -> int:
        return self.y + self.h


@dataclass(frozen=True)
class Monitor:
    name: str
    bounds: Rect
    work: Rect  # the part not covered by the taskbar
    primary: bool


@dataclass(frozen=True)
class Window:
    hwnd: int
    pid: int
    title: str
    cls: str = ""
    exe: str = ""


def order_screens(monitors: list[Monitor], *, swap: bool = False) -> list[Monitor]:
    """'Screen 1' is the primary monitor, 'Screen 2' the next one to its right/above (by position), and so on."""
    ordered = sorted(monitors, key=lambda m: (not m.primary, m.bounds.x, m.bounds.y))
    if swap and len(ordered) >= 2:
        ordered[0], ordered[1] = ordered[1], ordered[0]
    return ordered


def split_rect(rect: Rect, index: int, parts: int = 2) -> Rect:
    """Side-by-side halves, for a computer with a single monitor."""
    width = rect.w // parts
    return Rect(rect.x + index * width, rect.y, width, rect.h)


class Desktop:
    """Windows implementation. Every method is best-effort: a failure returns an empty/False result."""

    def __init__(self) -> None:
        if not IS_WINDOWS:
            raise OSError("Focus mode needs Windows.")
        import ctypes
        from ctypes import wintypes

        self._ct = ctypes
        self._wt = wintypes
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
        u = self.user32
        u.GetForegroundWindow.restype = wintypes.HWND
        u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u.IsWindowVisible.argtypes = [wintypes.HWND]
        u.IsIconic.argtypes = [wintypes.HWND]
        u.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        u.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
        u.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
        u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
        u.SetForegroundWindow.argtypes = [wintypes.HWND]
        u.BringWindowToTop.argtypes = [wintypes.HWND]
        u.IsWindow.argtypes = [wintypes.HWND]
        self._process_names: dict[int, tuple[float, str]] = {}

    # ------------------------------------------------------------------ DPI
    @contextmanager
    def physical_pixels(self) -> Iterator[None]:
        """Per-monitor DPI awareness for this thread only (so we never disturb the host app's own setting)."""
        previous = None
        try:
            fn = self.user32.SetThreadDpiAwarenessContext
            fn.restype = self._ct.c_void_p
            fn.argtypes = [self._ct.c_void_p]
            previous = fn(self._ct.c_void_p(-4))  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        except (AttributeError, OSError):
            pass
        try:
            yield
        finally:
            if previous:
                try:
                    self.user32.SetThreadDpiAwarenessContext(self._ct.c_void_p(previous))
                except OSError:
                    pass

    # ------------------------------------------------------------------ monitors
    def monitors(self) -> list[Monitor]:
        ct, wt = self._ct, self._wt

        class INFO(ct.Structure):
            _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD), ("szDevice", wt.WCHAR * 32)]

        found: list[Monitor] = []
        callback_type = ct.WINFUNCTYPE(ct.c_int, wt.HMONITOR, wt.HDC, ct.POINTER(wt.RECT), wt.LPARAM)

        def visit(handle, _dc, _rect, _param) -> int:
            info = INFO()
            info.cbSize = ct.sizeof(INFO)
            if self.user32.GetMonitorInfoW(handle, ct.byref(info)):
                m, w = info.rcMonitor, info.rcWork
                found.append(Monitor(
                    info.szDevice,
                    Rect(m.left, m.top, m.right - m.left, m.bottom - m.top),
                    Rect(w.left, w.top, w.right - w.left, w.bottom - w.top),
                    bool(info.dwFlags & 1),
                ))
            return 1

        with self.physical_pixels():
            self.user32.EnumDisplayMonitors(None, None, callback_type(visit), 0)
        return found

    # ------------------------------------------------------------------ windows
    def _describe(self, hwnd: int) -> Window | None:
        ct, wt = self._ct, self._wt
        pid = wt.DWORD(0)
        self.user32.GetWindowThreadProcessId(hwnd, ct.byref(pid))
        length = self.user32.GetWindowTextLengthW(hwnd)
        title = ""
        if length:
            buffer = ct.create_unicode_buffer(length + 1)
            self.user32.GetWindowTextW(hwnd, buffer, length + 1)
            title = buffer.value
        cls = ct.create_unicode_buffer(256)
        self.user32.GetClassNameW(hwnd, cls, 256)
        return Window(int(hwnd), int(pid.value), title, cls.value, self.process_name(int(pid.value)))

    def process_name(self, pid: int) -> str:
        cached = self._process_names.get(pid)
        now = time.monotonic()
        if cached and now - cached[0] < 30:
            return cached[1]
        name = ""
        try:
            import psutil

            name = psutil.Process(pid).name().lower()
        except Exception:
            pass
        self._process_names[pid] = (now, name)
        return name

    def foreground(self) -> Window | None:
        hwnd = self.user32.GetForegroundWindow()
        return self._describe(hwnd) if hwnd else None

    def _is_app_window(self, hwnd: int) -> bool:
        """A window a person would call "an open app": visible, titled, not a tool window, not a suspended ghost."""
        u = self.user32
        if not u.IsWindowVisible(hwnd) or u.GetWindow(hwnd, 4):  # GW_OWNER: dialogs belong to their app
            return False
        style = u.GetWindowLongW(hwnd, -20) & 0xFFFFFFFF  # GWL_EXSTYLE
        if style & 0x80 and not style & 0x40000:  # WS_EX_TOOLWINDOW without WS_EX_APPWINDOW
            return False
        cloaked = self._wt.DWORD(0)
        try:
            self.dwmapi.DwmGetWindowAttribute(hwnd, 14, self._ct.byref(cloaked), self._ct.sizeof(cloaked))  # DWMWA_CLOAKED
        except OSError:
            pass
        return not cloaked.value and u.GetWindowTextLengthW(hwnd) > 0

    def app_windows(self) -> list[Window]:
        """Open, visible, un-minimised app windows, front to back."""
        found: list[Window] = []
        callback_type = self._ct.WINFUNCTYPE(self._ct.c_int, self._wt.HWND, self._wt.LPARAM)

        def visit(hwnd, _param) -> int:
            try:
                if self._is_app_window(hwnd) and not self.user32.IsIconic(hwnd):
                    window = self._describe(hwnd)
                    if window:
                        found.append(window)
            except OSError:
                pass
            return 1

        self.user32.EnumWindows(callback_type(visit), 0)
        return found

    def minimize(self, hwnd: int) -> None:
        self.user32.ShowWindowAsync(hwnd, 6)  # SW_MINIMIZE, without waiting for a hung app

    def activate(self, hwnd: int) -> None:
        """Bring a window to the front. Windows only lets the foreground app do that, so tap Alt first (the usual trick)."""
        u = self.user32
        if not u.IsWindow(hwnd):
            return
        if u.GetAsyncKeyState(0x12) & 0x8000:  # you are holding Alt (mid Alt-Tab): a fake Alt release would cancel your switch
            return
        if u.IsIconic(hwnd):
            u.ShowWindow(hwnd, 9)  # SW_RESTORE
        u.keybd_event(0x12, 0, 0, 0)  # Alt down/up: marks us as having just had input
        u.keybd_event(0x12, 0, 2, 0)
        u.BringWindowToTop(hwnd)
        u.SetForegroundWindow(hwnd)

    def place(self, hwnd: int, rect: Rect, *, maximize: bool = True) -> None:
        """Move a window onto a monitor and (by default) maximise it there."""
        u = self.user32
        with self.physical_pixels():
            u.ShowWindow(hwnd, 9)  # restore first: a maximised window ignores SetWindowPos
            u.SetWindowPos(hwnd, None, rect.x, rect.y, rect.w, rect.h, 0x0004 | 0x0010)  # NOZORDER | NOACTIVATE
            if maximize:
                u.ShowWindow(hwnd, 3)  # SW_MAXIMIZE: fills the monitor the window is now on


def create_desktop() -> Desktop | None:
    try:
        return Desktop()
    except OSError:
        return None
