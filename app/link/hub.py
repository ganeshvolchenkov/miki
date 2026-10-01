"""The server end of the link. Listens on loopback only and keeps at most one laptop connection per role.

A newer connection for a role replaces the older one (the laptop reconnected after sleep, or you reopened the
dashboard), so a half-dead socket can never hold a role hostage.
"""

from __future__ import annotations

import logging
import socket
import threading
from typing import Any, Callable

from app.link.protocol import IDLE_SECONDS, ROLES, Connection, server_handshake

logger = logging.getLogger(__name__)

HANDSHAKE_SECONDS = 10
MAX_PENDING = 8  # connections still in the handshake; more are refused (nothing but the tunnel should knock)

ConnectHandler = Callable[[str, Connection], None]
MessageHandler = Callable[[str, dict[str, Any]], None]


class LinkHub:
    def __init__(self, token: str, *, host: str = "127.0.0.1", port: int = 0) -> None:
        if not token:
            raise ValueError("The link needs MIKI_LINK_TOKEN")
        self._token = token
        self._host = host
        self._port = port
        self._server: socket.socket | None = None
        self._lock = threading.Lock()
        self._conns: dict[str, Connection] = {}
        self._pending = threading.BoundedSemaphore(MAX_PENDING)
        self._on_connect: dict[str, list[ConnectHandler]] = {role: [] for role in ROLES}
        self._on_disconnect: dict[str, list[Callable[[], None]]] = {role: [] for role in ROLES}
        self._on_message: dict[str, list[MessageHandler]] = {role: [] for role in ROLES}
        self._stop = threading.Event()

    # ------------------------------------------------------------------ wiring
    def on_connect(self, role: str, handler: ConnectHandler) -> None:
        self._on_connect[role].append(handler)

    def on_disconnect(self, role: str, handler: Callable[[], None]) -> None:
        self._on_disconnect[role].append(handler)

    def on_message(self, role: str, handler: MessageHandler) -> None:
        self._on_message[role].append(handler)

    # ------------------------------------------------------------------ lifecycle
    @property
    def port(self) -> int:
        return self._server.getsockname()[1] if self._server is not None else self._port

    def start(self) -> None:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((self._host, self._port))
        server.listen(8)
        self._server = server
        threading.Thread(target=self._accept_loop, name="miki-link-accept", daemon=True).start()
        logger.info("Link hub listening on %s:%d", self._host, self.port)

    def stop(self) -> None:
        self._stop.set()
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
        with self._lock:
            conns, self._conns = list(self._conns.values()), {}
        for conn in conns:
            conn.close()

    # ------------------------------------------------------------------ talking
    def connected(self, role: str) -> bool:
        conn = self._conns.get(role)
        return conn is not None and not conn.closed

    def state(self, role: str) -> dict[str, Any]:
        conn = self._conns.get(role)
        return dict(conn.state) if conn is not None and not conn.closed else {}

    def send(self, role: str, message: dict[str, Any]) -> bool:
        conn = self._conns.get(role)
        return conn is not None and conn.send(message)

    # ------------------------------------------------------------------ internals
    def _accept_loop(self) -> None:
        assert self._server is not None
        while not self._stop.is_set():
            try:
                sock, _address = self._server.accept()
            except OSError:
                if self._stop.is_set():
                    return
                continue
            if not self._pending.acquire(blocking=False):
                sock.close()
                continue
            threading.Thread(target=self._serve, args=(sock,), name="miki-link-conn", daemon=True).start()

    def _serve(self, sock: socket.socket) -> None:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.settimeout(HANDSHAKE_SECONDS)
        conn = Connection(sock)
        try:
            ok = server_handshake(conn, self._token)
        finally:
            self._pending.release()
        if not ok:
            conn.close()
            return
        sock.settimeout(IDLE_SECONDS)
        conn.name = ",".join(conn.roles)
        for role in conn.roles:
            with self._lock:
                old = self._conns.get(role)
                self._conns[role] = conn
            if old is not None and old is not conn:
                old.close()
        logger.info("Link: %s connected", conn.name)
        for role in conn.roles:
            self._fire(self._on_connect[role], role, conn)
        try:
            while True:
                message = conn.recv()
                if message is None:
                    break
                if message["type"] == "ping":
                    conn.send({"type": "pong"})
                    continue
                for role in conn.roles:
                    for handler in self._on_message[role]:
                        try:
                            handler(role, message)
                        except Exception:
                            logger.exception("Link handler for %s failed on %s", role, message.get("type"))
        finally:
            conn.close()
            for role in conn.roles:
                with self._lock:
                    current = self._conns.get(role) is conn
                    if current:
                        del self._conns[role]
                if current:  # a replaced connection going away is not a disconnect
                    logger.info("Link: %s disconnected", role)
                    self._fire(self._on_disconnect[role])

    @staticmethod
    def _fire(handlers: list[Callable[..., None]], *args: Any) -> None:
        for handler in handlers:
            try:
                handler(*args)
            except Exception:
                logger.exception("Link event handler failed")
