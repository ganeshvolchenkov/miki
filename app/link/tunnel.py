"""The SSH tunnel from the laptop to the server: ``ssh -N -L 127.0.0.1:18765:127.0.0.1:8765 miki@<server>``.

It reuses the key you already log in with, so the server needs no new open port and no certificate. ``BatchMode``
means ssh never stops to ask a question: a missing key, a passphrase, or an unknown server key makes it fail with a
message we can show, instead of hanging invisibly.
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from app.link.protocol import LinkError

logger = logging.getLogger(__name__)

CREATE_NO_WINDOW = 0x08000000
UP_WAIT_SECONDS = 15


def find_ssh() -> str | None:
    found = shutil.which("ssh")
    if found:
        return found
    if sys.platform == "win32":
        builtin = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "OpenSSH" / "ssh.exe"
        if builtin.exists():
            return str(builtin)
    return None


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


class SshTunnel:
    def __init__(self, target: str, *, local_port: int, remote_port: int, ssh_port: int | None = None, key: str | None = None) -> None:
        self.target = target  # user@host
        self.local_port = local_port
        self.remote_port = remote_port
        self.ssh_port = ssh_port
        self.key = key
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._stderr = ""

    def command(self, ssh: str) -> list[str]:
        cmd = [
            ssh, "-N", "-T",
            "-o", "BatchMode=yes",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=3",
            "-o", "ConnectTimeout=10",
            "-L", f"127.0.0.1:{self.local_port}:127.0.0.1:{self.remote_port}",
        ]
        if self.ssh_port:
            cmd += ["-p", str(self.ssh_port)]
        if self.key:
            cmd += ["-i", self.key]
        return cmd + [self.target]

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def ensure(self) -> None:
        """Make sure the tunnel is up (start or restart ssh). Raises LinkError with a readable reason when it can't."""
        with self._lock:
            if self.running and port_open(self.local_port):
                return
            self._close_locked()
            ssh = find_ssh()
            if ssh is None:
                raise LinkError("ssh isn't installed. On Windows: Settings > System > Optional features > OpenSSH Client.")
            try:
                self._proc = subprocess.Popen(
                    self.command(ssh), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                    creationflags=CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                )
            except OSError as exc:
                raise LinkError(f"Couldn't start ssh: {exc}") from exc
            proc = self._proc
            deadline = time.monotonic() + UP_WAIT_SECONDS
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    err = proc.stderr.read().decode(errors="replace").strip() if proc.stderr else ""
                    self._proc = None
                    raise LinkError(explain_ssh_error(err, self.target))
                if port_open(self.local_port):
                    threading.Thread(target=self._drain, args=(proc,), daemon=True).start()
                    logger.info("SSH tunnel to %s is up (local port %d)", self.target, self.local_port)
                    return
                time.sleep(0.3)
            self._close_locked()
            raise LinkError(f"The SSH tunnel to {self.target} didn't come up in {UP_WAIT_SECONDS} s.")

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    def _close_locked(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    @staticmethod
    def _drain(proc: subprocess.Popen) -> None:
        """Keep reading ssh's stderr so it can never block on a full pipe; log what it says."""
        if proc.stderr is None:
            return
        for raw in proc.stderr:
            line = raw.decode(errors="replace").strip()
            if line:
                logger.info("ssh: %s", line)


def explain_ssh_error(stderr: str, target: str) -> str:
    text = stderr.lower()
    if "permission denied" in text:
        return (f"The server refused the SSH key for {target}. Check that `ssh {target}` works in PowerShell without "
                "typing anything (a key with a passphrase needs ssh-agent, or set MIKI_SSH_KEY to another key).")
    if "host key verification failed" in text:
        return f"This laptop doesn't know the server's key yet. Run `ssh {target}` once in PowerShell and answer yes."
    if "address already in use" in text or "cannot listen" in text:
        return "The local link port is taken by another program. Set MIKI_LINK_LOCAL_PORT to a free port."
    if "could not resolve" in text or "timed out" in text or "unreachable" in text or "refused" in text:
        return f"Can't reach the server {target} (is the laptop online?)."
    return f"ssh failed: {stderr[-300:] or 'no reason given'}"
