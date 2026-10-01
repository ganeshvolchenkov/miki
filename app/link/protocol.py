"""The wire format: one JSON object per line, over a TCP socket.

    server -> {"type": "hello", "version": 1, "nonce": "<hex>"}
    client -> {"type": "auth", "roles": ["hands"], "mac": HMAC-SHA256(token, nonce + "|" + roles), "state": {...}}
    server -> {"type": "welcome"}                (or it closes the connection)

After that either side may send any message. The client pings every ``PING_SECONDS``; a side that hears nothing for
``IDLE_SECONDS`` drops the connection. The token itself never crosses the wire.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import socket
import threading
from typing import Any, Callable

logger = logging.getLogger(__name__)

VERSION = 1
PING_SECONDS = 20
IDLE_SECONDS = 65
MAX_LINE = 8 * 1024 * 1024  # the brain graph is the biggest message (a few hundred KB)
MIN_TOKEN_LENGTH = 24
ROLES = ("dashboard", "hands")
DEFAULT_SERVER_PORT = 8765
DEFAULT_LOCAL_PORT = 18765


class LinkError(Exception):
    pass


def link_token() -> str:
    """MIKI_LINK_TOKEN, or "" when it is missing or too short to be safe."""
    token = os.getenv("MIKI_LINK_TOKEN", "").strip()
    return token if len(token) >= MIN_TOKEN_LENGTH else ""


def new_token() -> str:
    return secrets.token_urlsafe(32)


def env_port(name: str, default: int) -> int:
    try:
        port = int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default
    return port if 0 < port < 65536 else default


def sign(token: str, nonce: str, roles: list[str]) -> str:
    message = f"{nonce}|{','.join(roles)}".encode()
    return hmac.new(token.encode(), message, hashlib.sha256).hexdigest()


def verify(token: str, nonce: str, roles: list[str], mac: str) -> bool:
    return bool(token) and hmac.compare_digest(sign(token, nonce, roles), str(mac or ""))


class Connection:
    """A socket that sends and receives JSON lines. ``send`` is thread-safe; one thread should call ``recv``."""

    def __init__(self, sock: socket.socket, *, name: str = "") -> None:
        self.sock = sock
        self.name = name
        self.roles: tuple[str, ...] = ()
        self.state: dict[str, Any] = {}
        self._reader = sock.makefile("rb")
        self._send_lock = threading.Lock()
        self._closed = threading.Event()

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    def send(self, message: dict[str, Any]) -> bool:
        """Send one message. False (never an exception) when the connection is gone."""
        if self.closed:
            return False
        data = (json.dumps(message, separators=(",", ":")) + "\n").encode()
        try:
            with self._send_lock:
                self.sock.sendall(data)
            return True
        except OSError:
            self.close()
            return False

    def recv(self) -> dict[str, Any] | None:
        """The next message, or None when the connection closed, went quiet for too long, or sent garbage."""
        try:
            line = self._reader.readline(MAX_LINE + 1)
        except (OSError, ValueError):
            self.close()
            return None
        if not line or len(line) > MAX_LINE or not line.endswith(b"\n"):
            self.close()
            return None
        try:
            message = json.loads(line)
        except ValueError:
            logger.warning("Link %s sent something that isn't JSON; dropping it", self.name or "?")
            self.close()
            return None
        if not isinstance(message, dict) or not isinstance(message.get("type"), str):
            self.close()
            return None
        return message

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        for closer in (lambda: self.sock.shutdown(socket.SHUT_RDWR), self.sock.close, self._reader.close):
            try:
                closer()
            except OSError:
                pass


def client_handshake(conn: Connection, token: str, roles: list[str], state: dict[str, Any] | None = None) -> None:
    """Prove we know the token. Raises LinkError when the server doesn't let us in."""
    hello = conn.recv()
    if hello is None or hello.get("type") != "hello" or not isinstance(hello.get("nonce"), str):
        raise LinkError("The server didn't say hello (is Miki's brain running on the server?)")
    if hello.get("version") != VERSION:
        raise LinkError(f"The server speaks link version {hello.get('version')}, this laptop speaks {VERSION}. Update both.")
    conn.send({"type": "auth", "roles": roles, "mac": sign(token, hello["nonce"], roles), "state": state or {}})
    answer = conn.recv()
    if answer is None or answer.get("type") != "welcome":
        raise LinkError("The server refused this laptop. Check that MIKI_LINK_TOKEN is the same in both .env files.")


def server_handshake(conn: Connection, token: str) -> bool:
    """Check a newcomer. On success ``conn.roles`` and ``conn.state`` are filled in."""
    nonce = secrets.token_hex(16)
    if not conn.send({"type": "hello", "version": VERSION, "nonce": nonce}):
        return False
    auth = conn.recv()
    if auth is None or auth.get("type") != "auth":
        return False
    roles = auth.get("roles")
    if not isinstance(roles, list) or not roles or any(role not in ROLES for role in roles):
        return False
    if not verify(token, nonce, roles, auth.get("mac")):
        logger.warning("A link connection used the wrong token; closed it")
        return False
    state = auth.get("state")
    conn.roles = tuple(roles)
    conn.state = state if isinstance(state, dict) else {}
    return conn.send({"type": "welcome"})


Handler = Callable[[dict[str, Any]], None]
