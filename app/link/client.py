"""The laptop end of the link: connect, prove the token, keep the line alive, reconnect forever."""

from __future__ import annotations

import logging
import socket
import threading
from typing import Any, Callable

from app.link.protocol import IDLE_SECONDS, PING_SECONDS, Connection, LinkError, client_handshake

logger = logging.getLogger(__name__)

RETRY_SECONDS = (1, 2, 3, 5, 8, 13)  # then every 13 s


class LinkClient:
    def __init__(
        self,
        address: tuple[str, int],
        token: str,
        roles: list[str],
        *,
        on_message: Callable[[dict[str, Any]], None],
        on_status: Callable[[bool, str], None] = lambda up, why: None,
        state: Callable[[], dict[str, Any]] = dict,
        before_connect: Callable[[], None] = lambda: None,
    ) -> None:
        self.address = address
        self._token = token
        self._roles = roles
        self._on_message = on_message
        self._on_status = on_status
        self._state = state
        self._before_connect = before_connect  # e.g. make sure the SSH tunnel is up
        self._conn: Connection | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error = ""

    @property
    def connected(self) -> bool:
        return self._conn is not None and not self._conn.closed

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="miki-link-client", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._conn is not None:
            self._conn.close()

    def send(self, message: dict[str, Any]) -> bool:
        conn = self._conn
        return conn is not None and conn.send(message)

    # ------------------------------------------------------------------ internals
    def _run(self) -> None:
        failures = 0
        while not self._stop.is_set():
            error = self._session()
            if self._stop.is_set():
                return
            if error is None:
                failures = 0  # we were connected and the line dropped: try again right away
            else:
                failures += 1
                if error != self.last_error:
                    logger.info("Link to %s:%d: %s", *self.address, error)
                self.last_error = error
            self._on_status(False, error or "The connection dropped. Reconnecting…")
            self._stop.wait(RETRY_SECONDS[min(failures, len(RETRY_SECONDS) - 1)] if failures else 1)

    def _session(self) -> str | None:
        """One connection from start to end. Returns why it could not connect, or None after a normal drop."""
        try:
            self._before_connect()
            sock = socket.create_connection(self.address, timeout=10)
        except OSError as exc:
            return f"Can't reach Miki's brain ({exc.__class__.__name__})."
        except LinkError as exc:
            return str(exc)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        conn = Connection(sock, name="server")
        try:
            client_handshake(conn, self._token, self._roles, self._state())
        except LinkError as exc:
            conn.close()
            return str(exc)
        sock.settimeout(IDLE_SECONDS)
        self._conn = conn
        self.last_error = ""
        logger.info("Link to Miki's brain is up (%s)", ",".join(self._roles))
        self._on_status(True, "")
        pinger = threading.Thread(target=self._ping, args=(conn,), name="miki-link-ping", daemon=True)
        pinger.start()
        try:
            while True:
                message = conn.recv()
                if message is None:
                    break
                if message["type"] == "pong":
                    continue
                try:
                    self._on_message(message)
                except Exception:
                    logger.exception("Handling %s from the brain failed", message.get("type"))
        finally:
            conn.close()
            self._conn = None
        return None

    def _ping(self, conn: Connection) -> None:
        while not conn.closed and not self._stop.wait(PING_SECONDS):
            if not conn.send({"type": "ping"}):
                return
