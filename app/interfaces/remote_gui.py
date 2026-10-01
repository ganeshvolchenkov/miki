"""The dashboard window when Miki's brain lives on the server (MIKI_SERVER is set in .env).

The window, the page and the mascot are exactly the local dashboard's. What differs is who answers: every message you
type goes over the link to the brain on the server, and the brain paints this page back (chat bubbles, widgets, the
face). Nothing here thinks or remembers, so there is never a second memory on the laptop.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
import time
import webbrowser
from typing import Any

from app.interfaces.webview_host import WINDOW_TITLE, configure_webview2, create_local_server, webview_profile_dir
from app.link.client import LinkClient
from app.link.protocol import LinkError, link_token
from app.link.tunnel import port_open

logger = logging.getLogger(__name__)

SAFE_URL_PREFIXES = ("https://mail.google.com/",)  # the only pages the brain may open on this laptop
AGENT_WAIT_SECONDS = 25
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008


class ThinApi:
    """What the page calls (``window.pywebview.api``): each call is passed to the brain, nothing is done here."""

    def __init__(self) -> None:
        self.link: LinkClient | None = None

    def _call(self, method: str, value: str) -> None:
        if self.link is None or not self.link.send({"type": "call", "method": method, "args": [str(value or "")]}):
            self.window_js(f"appendMessage('system', {json.dumps(OFFLINE_TEXT)})")

    def process_message(self, text: str) -> None:
        if (text or "").strip():
            self._call("process_message", text)

    def open_mail(self, thread_id: str) -> bool:
        self._call("open_mail", thread_id)
        return True

    def dismiss_mail(self, message_id: str) -> None:
        self._call("dismiss_mail", message_id)

    window_js = staticmethod(lambda code: None)  # replaced once the window exists


OFFLINE_TEXT = "I can't reach my brain on the server right now, so I can't answer yet. I'll reconnect by myself."


class RemoteDashboard:
    def __init__(self, window: Any, api: ThinApi) -> None:
        self.window = window
        self.api = api
        self._was_up: bool | None = None
        self._last_reason = ""

    def js(self, code: str) -> None:
        try:
            self.window.evaluate_js(code)
        except Exception:
            logger.debug("evaluate_js failed", exc_info=True)

    def on_message(self, message: dict[str, Any]) -> None:
        kind = message["type"]
        if kind == "js" and isinstance(message.get("code"), str):
            self.js(message["code"])
        elif kind == "open_url":
            url = str(message.get("url", ""))
            if url.startswith(SAFE_URL_PREFIXES):
                webbrowser.open(url)
            else:
                logger.warning("The brain asked to open a page I don't open: %r", url[:80])

    def on_status(self, up: bool, why: str) -> None:
        if up:
            if self._was_up is False:
                self.js(f"appendMessage('system', {json.dumps('Connected to my brain again.')})")
            self.js("setPill('memory', true, 'Brain: online')")
        else:
            self.js("setPill('memory', false, 'Brain: offline')")
            if why and why != self._last_reason:  # say each new reason once, not every retry
                self.js(f"appendMessage('system', {json.dumps(why)})")
        self._last_reason = "" if up else why
        self._was_up = up


def ensure_hands() -> None:
    """The hands agent owns the SSH tunnel: start it if it isn't running, then wait for the tunnel's local end."""
    from app.hands.agent import launch_command, link_address

    host, port = link_address()
    if port_open(port, host):
        return
    try:
        subprocess.Popen(
            launch_command(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=(CREATE_NO_WINDOW | DETACHED_PROCESS) if sys.platform == "win32" else 0, close_fds=True,
        )
    except OSError as exc:
        raise LinkError(f"Couldn't start Miki's hands agent: {exc}") from exc
    deadline = time.monotonic() + AGENT_WAIT_SECONDS
    while time.monotonic() < deadline:
        if port_open(port, host):
            return
        time.sleep(0.5)
    raise LinkError("The link to the server didn't come up. See data/hands.log on this laptop for why.")


def run_remote_gui() -> int:
    import webview

    from app.hands.agent import link_address

    token = link_token()
    if not token:
        print("MIKI_SERVER is set but MIKI_LINK_TOKEN isn't (it must match the server's). Make one with: python -m app.link token")
        return 1

    configure_webview2()
    httpd, port = create_local_server()
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    api = ThinApi()
    window = webview.create_window(
        WINDOW_TITLE, url=f"http://127.0.0.1:{port}/index.html", width=1440, height=920, min_size=(1180, 760),
        background_color="#050914", js_api=api,
    )
    dashboard = RemoteDashboard(window, api)
    api.window_js = dashboard.js
    link = LinkClient(
        link_address(), token, ["dashboard"], on_message=dashboard.on_message, on_status=dashboard.on_status,
        before_connect=ensure_hands,
    )
    api.link = link

    started = threading.Event()

    def loaded() -> None:
        dashboard.js("setPill('memory', false, 'Brain: connecting…')")
        if started.is_set():  # the page reloaded: ask the brain to paint it again
            link.send({"type": "repaint"})
            return
        started.set()
        link.start()

    window.events.loaded += loaded
    webview.start(private_mode=False, storage_path=str(webview_profile_dir()))
    link.stop()
    try:
        httpd.shutdown()
        httpd.server_close()
    except Exception:
        logger.debug("http server shutdown failed", exc_info=True)
    return 0
