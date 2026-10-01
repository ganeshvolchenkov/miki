"""The window plumbing shared by the local dashboard and the thin (server-brain) dashboard. Imports nothing heavy."""

from __future__ import annotations

import os
from pathlib import Path

# Chromium switches that keep the embedded browser lean. Measured on this app: software rendering
# (--disable-gpu) drops the WebView2 GPU process from ~200 MB to ~30 MB private memory, and the
# page is static at idle so it costs almost no CPU. Set MIKI_GPU=1 to use the GPU anyway, or set
# WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS yourself to override everything.
_WEBVIEW2_LEAN_ARGS = (
    "--renderer-process-limit=1 "
    "--disable-features=Translate,MediaRouter,OptimizationHints,msSmartScreenProtection "
    "--disable-background-networking --disable-component-update --disable-sync "
    '--js-flags="--lite-mode --max-old-space-size=64"'
)

WINDOW_TITLE = "Miki Command Center"


def configure_webview2() -> None:
    if "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS" in os.environ:
        return
    use_gpu = os.getenv("MIKI_GPU", "").strip().lower() in {"1", "true", "yes"}
    os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = ("" if use_gpu else "--disable-gpu ") + _WEBVIEW2_LEAN_ARGS


def webview_profile_dir() -> Path:
    """A fixed browser profile (instead of a fresh temp dir every launch), so Google Fonts and other
    cached assets survive restarts and nothing accumulates in %TEMP%."""
    path = Path("data/webview").resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def create_local_server():
    import http.server
    import socketserver

    web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=web_dir, **kwargs)

        def log_message(self, format, *args):
            pass

    class ThreadingHTTPServer(socketserver.TCPServer):
        allow_reuse_address = True

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = httpd.server_address[1]
    return httpd, port
