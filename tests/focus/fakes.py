from __future__ import annotations

from app.focus.chrome import Tab
from app.focus.winapi import Monitor, Rect, Window

PRIMARY = Monitor("\\\\.\\DISPLAY1", Rect(0, 0, 2560, 1600), Rect(0, 0, 2560, 1540), True)
SECOND = Monitor("\\\\.\\DISPLAY5", Rect(2560, -463, 1920, 1080), Rect(2560, -463, 1920, 1032), False)

FOCUS_CHROME_PID = 100


class FakeDesktop:
    """A pretend Windows desktop: a list of windows, one of them in front, and a log of what was done to them."""

    def __init__(self, windows=None, monitors=None):
        self.windows: list[Window] = list(windows or [])
        self._monitors = monitors if monitors is not None else [PRIMARY, SECOND]
        self.front: Window | None = None
        self.minimized: list[int] = []
        self.activated: list[int] = []
        self.placed: list[tuple[int, Rect]] = []

    def physical_pixels(self):
        from contextlib import nullcontext

        return nullcontext()

    def monitors(self):
        return list(self._monitors)

    def foreground(self):
        return self.front

    def app_windows(self):
        return [w for w in self.windows if w.hwnd not in self.minimized]

    def minimize(self, hwnd):
        self.minimized.append(hwnd)
        if self.front is not None and self.front.hwnd == hwnd:
            self.front = None

    def activate(self, hwnd):
        self.activated.append(hwnd)
        self.front = next((w for w in self.windows if w.hwnd == hwnd), None)

    def place(self, hwnd, rect, *, maximize=True):
        self.placed.append((hwnd, rect))


class FakeChrome:
    """Stands in for FocusChrome: a list of open tabs and a log of what the guard did to them."""

    def __init__(self, tabs=None, pids=(FOCUS_CHROME_PID,), running=True):
        self._tabs = [Tab(t) for t in (tabs or [])]
        self._pids = set(pids)
        self.running = running
        self.closed: list[str] = []
        self.opened: list[str] = []
        self.windows_opened: list[tuple] = []
        self.on_open_window = None

    def is_running(self):
        return self.running

    def browser_pids(self):
        return set(self._pids)

    def tabs(self):
        return list(self._tabs)

    def close_tab(self, tab_id):
        self.closed.append(tab_id)
        self._tabs = [t for t in self._tabs if t.id != tab_id]
        return True

    def open_tab(self, url):
        self.opened.append(url)
        self._tabs.append(Tab({"id": f"new{len(self.opened)}", "url": url, "type": "page"}))
        return True

    def open_window(self, url, x=None, y=None, w=None, h=None):
        self.running = True
        self.windows_opened.append((url, x, y, w, h))
        if self.on_open_window:
            self.on_open_window(url)


class FakePet:
    def __init__(self):
        self.calls: list[tuple] = []
        self.pid = 555
        self.running = False

    def start(self, floor):
        self.running = True
        self.calls.append(("start", floor))
        return True

    def timeline(self, start, end):
        self.calls.append(("timeline", start, end))

    def mood(self, mood):
        self.calls.append(("mood", mood))

    def say(self, text, seconds=6.0):
        self.calls.append(("say", text))

    def info(self, text):
        self.calls.append(("info", text))

    def stop(self):
        self.running = False
        self.calls.append(("stop",))

    def moods(self):
        return [c[1] for c in self.calls if c[0] == "mood"]

    def said(self):
        return [c[1] for c in self.calls if c[0] == "say"]


def tab(id, url, title=""):
    return {"id": id, "url": url, "title": title, "type": "page"}


def window(hwnd, exe, title="A window", pid=None, cls="Win"):
    return Window(hwnd, pid if pid is not None else hwnd * 10, title, cls, exe)
