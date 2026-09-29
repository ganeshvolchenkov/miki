"""The bouncer. Once a second during focus it checks three things, and touches only what is on the banned list:

* the program in front: a banned app (Discord, Steam ...) is minimised and a focus window is brought back;
* a browser we can't see into (your everyday Chrome, Edge, Firefox) showing a banned site in its title is minimised;
* the focus Chrome's tabs: any tab on a banned site is closed.

Nothing is ever killed and no system setting is changed. Minimising and closing a tab lose nothing, and the
moment Miki stops (or the round ends) the guard is simply gone, so it can never lock you out of your own computer.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

from app.focus import rules
from app.focus.winapi import Window

logger = logging.getLogger(__name__)

REPEAT_SECONDS = 4.0  # the same distraction inside this window counts once (minimising takes a moment)
CHROME = "chrome.exe"


@dataclass
class Bounce:
    kind: str  # "app" or "site"
    label: str  # "Discord", "youtube.com"


class FocusGuard:
    def __init__(
        self,
        desktop: Any,
        chrome: Any | None,
        *,
        banned_sites: Callable[[], tuple[str, ...]],
        banned_apps: Callable[[], frozenset[str]],
        own_pids: Callable[[], set[int]] = lambda: set(),
        home_url: str = "about:blank",
        on_bounce: Callable[[Bounce], None] = lambda bounce: None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.desktop = desktop
        self.chrome = chrome
        self.banned_sites = banned_sites
        self.banned_apps = banned_apps
        self.own_pids = own_pids
        self.home_url = home_url
        self.on_bounce = on_bounce
        self._clock = clock
        self._last: dict[str, float] = {}
        self._focus_pids: set[int] = set()
        self._focus_pids_at = float("-inf")

    # ------------------------------------------------------------------ who counts as "ours"
    def _focus_chrome_pids(self) -> set[int]:
        now = self._clock()
        if self.chrome is not None and now - self._focus_pids_at > 3.0:
            self._focus_pids_at = now
            try:
                self._focus_pids = set(self.chrome.browser_pids())
            except Exception:
                logger.debug("Could not list the focus Chrome", exc_info=True)
        return self._focus_pids

    def is_ours(self, window: Window) -> bool:
        """Miki itself, or the Miki-watched Chrome (its tabs are policed by URL, not by title)."""
        return window.pid in self.own_pids() or (window.exe == CHROME and window.pid in self._focus_chrome_pids())

    def verdict(self, window: Window) -> Bounce | None:
        """Is this window something to bounce? A Bounce describing why, or None."""
        if self.is_ours(window):
            return None
        if rules.app_banned(window.exe, self.banned_apps()):
            return Bounce("app", _app_label(window))
        if window.exe in rules.BROWSERS:
            site = rules.title_hit(window.title, self.banned_sites())
            if site:
                return Bounce("site", site)
        return None

    def focus_windows(self) -> list[Window]:
        """Windows to bring back: the focus Chrome windows."""
        return [w for w in self.desktop.app_windows() if w.exe == CHROME and w.pid in self._focus_chrome_pids()]

    # ------------------------------------------------------------------ the checks
    def _repeat(self, key: str) -> bool:
        now = self._clock()
        if now - self._last.get(key, -1e9) < REPEAT_SECONDS:
            return True
        self._last[key] = now
        return False

    def check_app(self) -> Bounce | None:
        front = self.desktop.foreground()
        if front is None:
            return None
        bounce = self.verdict(front)
        if bounce is None:
            return None
        self.desktop.minimize(front.hwnd)
        bring_back = self.focus_windows()
        if bring_back:
            self.desktop.activate(bring_back[0].hwnd)
        return None if self._repeat(f"{bounce.kind}:{bounce.label}:{front.hwnd}") else bounce

    def check_tabs(self) -> list[Bounce]:
        if self.chrome is None:
            return []
        tabs = self.chrome.tabs()
        banned = self.banned_sites()
        bad = [(tab, rules.judge_url(tab.url, banned)) for tab in tabs]
        bad = [(tab, verdict) for tab, verdict in bad if not verdict.allowed]
        bounces: list[Bounce] = []
        remaining = len(tabs) - len(bad)
        for tab, verdict in bad:
            if remaining == 0:  # closing the last tab would close the whole window: leave a study page in its place
                self.chrome.open_tab(self.home_url)
                remaining = 1
            if self.chrome.close_tab(tab.id):
                bounces.append(Bounce("site", verdict.label))
        return bounces

    def clean_slate(self) -> int:
        """At the start of focus: tuck away anything already open that is banned, so the screens start clear."""
        count = 0
        for window in self.desktop.app_windows():
            if self.verdict(window) is not None:
                self.desktop.minimize(window.hwnd)
                count += 1
        return count

    def tick(self) -> list[Bounce]:
        """One pass. Reports each bounce through ``on_bounce`` and returns them."""
        bounces: list[Bounce] = []
        for check in (self.check_app, self.check_tabs):
            try:
                result = check()
            except Exception:
                logger.debug("Guard check failed", exc_info=True)
                continue
            if isinstance(result, Bounce):
                bounces.append(result)
            elif result:
                bounces.extend(result)
        for bounce in bounces:
            self.on_bounce(bounce)
        return bounces


def _app_label(window: Window) -> str:
    """A friendly name for a program: 'Discord' from discord.exe, falling back to the window title."""
    name = window.exe[:-4] if window.exe.endswith(".exe") else window.exe
    return name.capitalize() if name else (window.title[:30] or "another app")
