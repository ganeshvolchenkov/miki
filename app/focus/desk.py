"""The desk: the part of focus mode that needs a real Windows screen.

It puts Gemini and Claude on the right monitors, tucks the clutter away, runs the bouncer and shows the pet. It has
no clock and no memory: whoever owns the round (``FocusService``, locally or on the server) tells it what to do.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable

from app.focus.chrome import FocusChrome, find_chrome
from app.focus.guard import Bounce, FocusGuard
from app.focus.pet_host import PetHost
from app.focus.winapi import Rect, Window, create_desktop, order_screens, split_rect

logger = logging.getLogger(__name__)


class FocusDesk:
    def __init__(
        self,
        config: Any,
        *,
        desktop: Any = None,
        chrome: Any = None,
        pet: Any = None,
        banned_sites: Callable[[], tuple[str, ...]],
        banned_apps: Callable[[], frozenset[str]],
        on_bounce: Callable[[Bounce], None],
    ) -> None:
        self.config = config
        self.desktop = desktop if desktop is not None else create_desktop()
        chrome_path = find_chrome(config.chrome_path)
        self.chrome = chrome if chrome is not None else (
            FocusChrome(chrome_path, config.profile_dir, port=config.port) if chrome_path else None
        )
        self.pet = pet if pet is not None else PetHost()
        self.guard = FocusGuard(
            self.desktop, self.chrome, banned_sites=banned_sites, banned_apps=banned_apps,
            own_pids=self.own_pids, home_url=config.screen2_url, on_bounce=on_bounce,
        ) if self.desktop is not None else None

    @property
    def available(self) -> bool:
        return self.desktop is not None

    def problem(self) -> str | None:
        """Why focus mode can't start here, or None."""
        if self.desktop is None or self.guard is None:
            return "Focus mode needs Windows."
        if self.chrome is None:
            return "I couldn't find Google Chrome. Install it, or set MIKI_CHROME_PATH in .env."
        return None

    def own_pids(self) -> set[int]:
        pids = {os.getpid()}
        pet_pid = getattr(self.pet, "pid", None)
        if pet_pid:
            pids.add(int(pet_pid))
        return pids

    # ------------------------------------------------------------------ bringing the screens up
    def set_up(self) -> None:
        """Windows on the right screens, clutter tucked away, pet out."""
        try:
            self.layout_windows()
        except Exception:
            logger.exception("Could not set up the focus screens")
        try:
            if self.guard is not None:
                self.guard.clean_slate()
        except Exception:
            logger.debug("clean slate failed", exc_info=True)
        self.start_pet()

    def start_pet(self) -> None:
        try:
            with self.desktop.physical_pixels():
                screens = order_screens(self.desktop.monitors(), swap=self.config.swap_screens)
            if screens:
                self.pet.start(screens[0].work)
        except Exception:
            logger.debug("pet start failed", exc_info=True)

    def focus_windows(self) -> list[Window]:
        pids = self.chrome.browser_pids()
        return [w for w in self.desktop.app_windows() if w.exe == "chrome.exe" and w.pid in pids]

    def layout_windows(self) -> None:
        with self.desktop.physical_pixels():
            screens = order_screens(self.desktop.monitors(), swap=self.config.swap_screens)
        if not screens:
            return
        if len(screens) >= 2:
            rects = [screens[0].work, screens[1].work]
        else:  # one monitor: side by side
            rects = [split_rect(screens[0].work, 0), split_rect(screens[0].work, 1)]
        urls = [self.config.screen1_url, self.config.screen2_url]

        existing = self.focus_windows() if self.chrome.is_running() else []
        gemini = next((w for w in existing if "gemini" in w.title.lower()), None)
        first = gemini or (existing[0] if existing else None)
        rest = [w for w in existing if w is not first]
        second = rest[0] if rest else None
        chosen: list[Window | None] = [first, second]

        for index, (url, rect) in enumerate(zip(urls, rects)):
            window = chosen[index]
            if window is None:
                window = self.open_window(url, rect)
            if window is not None:
                self.desktop.place(window.hwnd, rect)
        # leave Screen 1's window in front: that is where Gemini is
        front = chosen[0]
        if front is not None:
            self.desktop.activate(front.hwnd)

    def open_window(self, url: str, rect: Rect) -> Window | None:
        """Open ``url`` in a new focus-Chrome window and return it once Windows shows it."""
        before = {w.hwnd for w in self.focus_windows()} if self.chrome.is_running() else set()
        self.chrome.open_window(url, rect.x, rect.y, rect.w, rect.h)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            time.sleep(0.4)
            fresh = [w for w in self.focus_windows() if w.hwnd not in before]
            if fresh:
                return fresh[0]
        return None
