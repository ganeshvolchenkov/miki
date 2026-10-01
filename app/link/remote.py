"""Server-side stand-ins for things that live on the laptop: the dashboard window, and focus mode's hands and pet.

They look like the local objects the rest of Miki already talks to (a pywebview window, a ``PetHost``), so the WebApi
and ``FocusService`` run unchanged on the server and every call simply travels down the link.
"""

from __future__ import annotations

import logging
from typing import Any

from app.link.hub import LinkHub

logger = logging.getLogger(__name__)


class RemoteWindow:
    """What the WebApi calls ``window``: every ``evaluate_js`` goes to the dashboard on the laptop (if one is open)."""

    def __init__(self, hub: LinkHub) -> None:
        self._hub = hub

    @property
    def connected(self) -> bool:
        return self._hub.connected("dashboard")

    def evaluate_js(self, code: str) -> None:
        self._hub.send("dashboard", {"type": "js", "code": code})

    def open_url(self, url: str) -> bool:
        return self._hub.send("dashboard", {"type": "open_url", "url": url})


class RemotePet:
    """The pixel pet on the laptop's screen, driven from here."""

    def __init__(self, hub: LinkHub) -> None:
        self._hub = hub

    def _send(self, op: str, **args: Any) -> None:
        self._hub.send("hands", {"type": "focus.pet", "op": op, **args})

    @property
    def running(self) -> bool:
        return self._hub.connected("hands")

    pid = None

    def start(self, floor: Any = None) -> bool:
        self._send("start")
        return True

    def timeline(self, start: float, end: float) -> None:
        self._send("timeline", start=start, end=end)

    def mood(self, mood: str) -> None:
        self._send("mood", mood=mood)

    def say(self, text: str, seconds: float = 6.0) -> None:
        self._send("say", text=text, seconds=seconds)

    def info(self, text: str) -> None:
        self._send("info", text=text)

    def stop(self) -> None:
        self._send("stop")


class RemoteHands:
    """Focus mode's hands: the laptop that arranges the screens and runs the bouncer."""

    def __init__(self, hub: LinkHub) -> None:
        self._hub = hub
        self.pet = RemotePet(hub)

    @property
    def connected(self) -> bool:
        return self._hub.connected("hands")

    def problem(self) -> str | None:
        """Why a round can't start on the laptop right now, or None."""
        if not self.connected:
            return ("Your laptop isn't connected to me right now, so I can't set up your screens. "
                    "Switch it on and open Miki (or check the Miki Hands app is running), then /focus again.")
        problem = self._hub.state("hands").get("focus_problem")
        return str(problem) if problem else None

    def arm(self, sites: tuple[str, ...] | list[str], apps: list[str], until: float) -> None:
        self._hub.send("hands", {"type": "focus.arm", "sites": list(sites), "apps": list(apps), "until": until})

    def disarm(self) -> None:
        self._hub.send("hands", {"type": "focus.disarm"})

    def set_bans(self, sites: tuple[str, ...] | list[str], apps: list[str]) -> None:
        self._hub.send("hands", {"type": "focus.bans", "sites": list(sites), "apps": list(apps)})

    def set_until(self, until: float) -> None:
        self._hub.send("hands", {"type": "focus.until", "until": until})

    def beep(self) -> None:
        self._hub.send("hands", {"type": "focus.beep"})


def wire_focus(hub: LinkHub, focus: Any) -> None:
    """Connect a server-side ``FocusService`` (built with ``RemoteHands``) to the laptop's reports."""

    def connected(_role: str, conn: Any) -> None:
        focus.hands_connected(laptop_armed=bool(conn.state.get("focus_armed")))

    def message(_role: str, msg: dict[str, Any]) -> None:
        if msg["type"] == "focus.bounce":
            kind, label = str(msg.get("kind", "")), str(msg.get("label", ""))[:80]
            if kind in {"app", "site"} and label:
                focus.laptop_bounced(kind, label)

    hub.on_connect("hands", connected)
    hub.on_message("hands", message)


DASHBOARD_CALLS = {"process_message", "open_mail", "dismiss_mail"}


def wire_dashboard(hub: LinkHub, api: Any) -> None:
    """Connect the server-side WebApi to the dashboard window on the laptop. Only the three page actions may be called."""

    def message(_role: str, msg: dict[str, Any]) -> None:
        if msg["type"] == "repaint":
            api.dashboard_connected()
            return
        if msg["type"] != "call":
            return
        method, args = msg.get("method"), msg.get("args")
        if method not in DASHBOARD_CALLS or not isinstance(args, list) or len(args) != 1 or not isinstance(args[0], str):
            logger.warning("Dashboard asked for something it may not: %r", method)
            return
        getattr(api, method)(args[0][:20000])

    hub.on_connect("dashboard", lambda _role, _conn: api.dashboard_connected())
    hub.on_message("dashboard", message)
