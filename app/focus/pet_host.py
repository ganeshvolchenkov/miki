"""Starts and talks to the desktop pet process (see ``app.focus.pet``). Every call is best-effort: no pet is never an error."""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from app.focus.winapi import Rect

logger = logging.getLogger(__name__)

STRIP_HEIGHT = 150
CREATE_NO_WINDOW = 0x08000000


class PetHost:
    def __init__(self, project_dir: Path | None = None) -> None:
        self._project = project_dir or Path(__file__).resolve().parent.parent.parent
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self.running and self._proc is not None else None

    def start(self, floor: Rect) -> bool:
        """Show the pet walking along the bottom of ``floor`` (a monitor's work area)."""
        with self._lock:
            if self.running:
                return True
            try:
                self._proc = subprocess.Popen(
                    [sys.executable, "-m", "app.focus.pet", "--x", str(floor.x), "--y", str(floor.bottom - STRIP_HEIGHT),
                     "--w", str(floor.w), "--h", str(STRIP_HEIGHT)],
                    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True,
                    cwd=str(self._project), creationflags=CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                )
                return True
            except OSError:
                logger.warning("The desktop pet could not start", exc_info=True)
                self._proc = None
                return False

    def send(self, **message: Any) -> None:
        with self._lock:
            proc = self._proc
            if proc is None or proc.poll() is not None or proc.stdin is None:
                return
            try:
                proc.stdin.write(json.dumps(message) + "\n")
                proc.stdin.flush()
            except (OSError, ValueError):
                self._proc = None

    def timeline(self, start: float, end: float) -> None:
        """The round to draw as a progress bar: the pet stands at (now - start) / (end - start) of the way along."""
        self.send(cmd="timeline", start=start, end=end)

    def mood(self, mood: str) -> None:
        self.send(cmd="mood", mood=mood)

    def say(self, text: str, seconds: float = 6.0) -> None:
        self.send(cmd="say", text=text, seconds=seconds)

    def info(self, text: str) -> None:
        self.send(cmd="info", text=text)

    def stop(self) -> None:
        """Ask the pet to wave goodbye and leave; make sure it is gone a little later."""
        self.send(cmd="quit")
        with self._lock:
            proc, self._proc = self._proc, None
        if proc is not None:
            def reap() -> None:
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()

            threading.Thread(target=reap, daemon=True).start()
