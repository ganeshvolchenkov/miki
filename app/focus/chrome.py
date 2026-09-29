"""A Chrome that Miki can see into: its own profile, started with a local debugging port.

Why a separate profile? Chrome refuses to expose its tab list on the everyday profile, and Miki needs the tab
URLs to tell Gemini from YouTube. The focus profile lives in ``data/focus-chrome``: sign in to Gemini, Claude,
Gmail and Canvas there once and it remembers you, like any Chrome.

Only Chrome's ``/json`` HTTP endpoints on 127.0.0.1 are used (list tabs, open a tab, close a tab), so nothing
extra is installed and no websocket is needed.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

DEFAULT_PORT = 9333
CREATE_NO_WINDOW = 0x08000000


def find_chrome(explicit: str | None = None) -> Path | None:
    """Where Chrome is installed, or None."""
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    for var in ("ProgramFiles", "ProgramFiles(x86)", "LocalAppData"):
        base = os.environ.get(var)
        if base:
            candidates.append(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe")
    found = shutil.which("chrome")
    if found:
        candidates.append(Path(found))
    return next((c for c in candidates if c.is_file()), None)


class Tab(dict):
    """One entry of Chrome's ``/json/list`` (id, url, title, type ...)."""

    @property
    def id(self) -> str:
        return str(self.get("id", ""))

    @property
    def url(self) -> str:
        return str(self.get("url", ""))


class FocusChrome:
    def __init__(
        self,
        chrome_path: Path,
        profile_dir: Path,
        *,
        port: int = DEFAULT_PORT,
        opener: Callable[..., Any] | None = None,
        launcher: Callable[..., Any] | None = None,
    ) -> None:
        self.chrome_path = Path(chrome_path)
        self.profile_dir = Path(profile_dir).resolve()
        self.port = port
        self._open = opener or urllib.request.urlopen
        self._launch = launcher or subprocess.Popen

    # ------------------------------------------------------------------ the debugging endpoint
    def _request(self, path: str, *, method: str = "GET", timeout: float = 2.0) -> Any:
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method)
        with self._open(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
        try:
            return json.loads(body)
        except ValueError:
            return body

    def is_running(self) -> bool:
        try:
            info = self._request("/json/version", timeout=1.0)
            return isinstance(info, dict) and "Browser" in info
        except (OSError, urllib.error.URLError, ValueError):
            return False

    def wait_until_running(self, seconds: float = 15.0, *, sleep: Callable[[float], None] = time.sleep) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.is_running():
                return True
            sleep(0.3)
        return self.is_running()

    def tabs(self) -> list[Tab]:
        """Open pages only (not service workers, extensions' background pages ...). Empty if Chrome isn't reachable."""
        try:
            items = self._request("/json/list")
        except (OSError, urllib.error.URLError, ValueError):
            return []
        return [Tab(t) for t in items if isinstance(t, dict) and t.get("type") == "page"] if isinstance(items, list) else []

    def close_tab(self, tab_id: str) -> bool:
        if not tab_id or not all(ch.isalnum() or ch in "-_" for ch in tab_id):  # ids are hex/uuid; never build odd paths
            return False
        try:
            self._request(f"/json/close/{tab_id}")
            return True
        except (OSError, urllib.error.URLError):
            return False

    def open_tab(self, url: str) -> bool:
        try:
            self._request("/json/new?" + urllib.request.quote(url, safe=":/?=&%#"), method="PUT")
            return True
        except (OSError, urllib.error.URLError):
            return False

    # ------------------------------------------------------------------ windows
    def open_window(self, url: str, x: int | None = None, y: int | None = None, w: int | None = None, h: int | None = None) -> None:
        """Open ``url`` in a new window of the focus Chrome (starting Chrome if needed). Positioning is refined by Windows later."""
        args = [
            str(self.chrome_path),
            f"--user-data-dir={self.profile_dir}",
            f"--remote-debugging-port={self.port}",
            "--remote-debugging-address=127.0.0.1",
            "--no-first-run", "--no-default-browser-check", "--disable-session-crashed-bubble", "--hide-crash-restore-bubble",
            "--new-window",
        ]
        if x is not None and y is not None:
            args.append(f"--window-position={x},{y}")
        if w is not None and h is not None:
            args.append(f"--window-size={w},{h}")
        args.append(url)
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._launch(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)

    def browser_pids(self) -> set[int]:
        """Process ids of the focus Chrome's main process (the one that owns its windows)."""
        try:
            import psutil
        except ImportError:
            return set()
        marker = f"--user-data-dir={self.profile_dir}".lower()
        pids: set[int] = set()
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                if (proc.info["name"] or "").lower() != "chrome.exe":
                    continue
                cmd = [str(part) for part in (proc.info["cmdline"] or [])]
                if any(part.lower() == marker for part in cmd) and not any(part.startswith("--type=") for part in cmd):
                    pids.add(int(proc.info["pid"]))
            except (psutil.Error, OSError):
                continue
        return pids
