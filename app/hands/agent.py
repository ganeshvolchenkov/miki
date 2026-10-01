"""The hands agent: runs quietly on the laptop, keeps the SSH tunnel to Miki's brain open, and does the screen work.

    python -m app.hands                       run it (it also starts by itself when you open the dashboard)
    python -m app.hands --install-autostart   start it hidden when you log in to Windows
    python -m app.hands --remove-autostart

When the brain starts a focus round (you typed /focus on your phone, or in the dashboard) the agent puts Gemini and
Claude on your screens, runs the bouncer and shows the pet. It reports every bounce back so the brain can count them.

Failsafe: the agent is told when the round ends. If it loses the brain and that time passes, it stops guarding on its
own, so a dropped connection can never leave your laptop locked in focus mode.
"""

from __future__ import annotations

import logging
import os
import queue
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any

from app.core import single_instance
from app.focus import rules
from app.focus.desk import FocusDesk
from app.focus.guard import Bounce
from app.focus.service import FocusConfig
from app.link.client import LinkClient
from app.link.protocol import DEFAULT_LOCAL_PORT, DEFAULT_SERVER_PORT, env_port, link_token
from app.link.tunnel import SshTunnel

logger = logging.getLogger(__name__)

LOCK_NAME = "MikiHands"
STARTUP_FILE = "Miki Hands.vbs"
GRACE_SECONDS = 120  # past the round's end with no word from the brain: stop guarding anyway
TICK_SECONDS = 1.0
MAX_BANS = 500
PET_OPS = {"start", "timeline", "mood", "say", "info", "stop"}


def _clean_list(value: Any, *, apps: bool = False) -> list[str] | None:
    if not isinstance(value, list) or len(value) > MAX_BANS:
        return None
    out = []
    for item in value:
        if not isinstance(item, str) or not item or len(item) > 120:
            return None
        item = item.strip().lower()
        if apps and not item.endswith(".exe"):
            return None
        out.append(item)
    return out


class HandsAgent:
    def __init__(self, config: FocusConfig | None = None, *, desk: Any = None, beep: Any = None) -> None:
        self.config = config or FocusConfig.from_env()
        self._sites: tuple[str, ...] = tuple(rules.DEFAULT_BANNED_SITES)
        self._apps: frozenset[str] = frozenset(rules.DEFAULT_BANNED_APPS)
        self.desk = desk if desk is not None else FocusDesk(
            self.config, banned_sites=lambda: self._sites, banned_apps=lambda: self._apps, on_bounce=self._bounced,
        )
        self._beep = beep or _beep
        self.armed = threading.Event()
        self.until = 0.0
        self.link: LinkClient | None = None
        self._jobs: queue.Queue[dict[str, Any]] = queue.Queue()
        self._stop = threading.Event()

    # ------------------------------------------------------------------ what the brain sees
    def state(self) -> dict[str, Any]:
        return {
            "focus_armed": self.armed.is_set(),
            "focus_problem": self.desk.problem() if self.config.enabled else "Focus mode is switched off on this laptop (MIKI_FOCUS=0).",
            "host": socket.gethostname(),
        }

    def _bounced(self, bounce: Bounce) -> None:
        if self.link is not None:
            self.link.send({"type": "focus.bounce", "kind": bounce.kind, "label": bounce.label})

    # ------------------------------------------------------------------ running
    def start(self, link: LinkClient) -> None:
        self.link = link
        threading.Thread(target=self._work, name="miki-hands-work", daemon=True).start()
        threading.Thread(target=self._guard_loop, name="miki-hands-guard", daemon=True).start()
        link.start()

    def stop(self) -> None:
        self._stop.set()
        self.armed.clear()
        if self.link is not None:
            self.link.stop()
        self.desk.pet.stop()

    def on_message(self, message: dict[str, Any]) -> None:
        """From the link's reader thread: queue it, so a slow window set-up never holds up the connection."""
        if message["type"].startswith("focus."):
            self._jobs.put(message)

    def _work(self) -> None:
        while not self._stop.is_set():
            message = self._jobs.get()
            try:
                self.handle(message)
            except Exception:
                logger.exception("Hands: %s failed", message.get("type"))

    def handle(self, message: dict[str, Any]) -> None:
        kind = message["type"]
        if kind == "focus.arm":
            sites, apps = _clean_list(message.get("sites")), _clean_list(message.get("apps"), apps=True)
            until = message.get("until")
            if sites is None or apps is None or not isinstance(until, (int, float)):
                logger.warning("Hands: ignored a malformed focus.arm")
                return
            self._sites, self._apps, self.until = tuple(sites), frozenset(apps), float(until)
            self.armed.clear()
            if self.desk.problem():
                return
            self.desk.set_up()
            if time.time() < self.until:
                self.armed.set()
        elif kind == "focus.disarm":
            self.armed.clear()
        elif kind == "focus.bans":
            sites, apps = _clean_list(message.get("sites")), _clean_list(message.get("apps"), apps=True)
            if sites is not None and apps is not None:
                self._sites, self._apps = tuple(sites), frozenset(apps)
        elif kind == "focus.until":
            if isinstance(message.get("until"), (int, float)):
                self.until = float(message["until"])
        elif kind == "focus.beep":
            self._beep()
        elif kind == "focus.pet":
            self._pet(message)

    def _pet(self, message: dict[str, Any]) -> None:
        op, pet = message.get("op"), self.desk.pet
        if op not in PET_OPS:
            return
        if op == "start":
            if self.desk.desktop is not None:
                self.desk.start_pet()
        elif op == "stop":
            pet.stop()
        elif op == "timeline":
            start, end = message.get("start"), message.get("end")
            if isinstance(start, (int, float)) and isinstance(end, (int, float)):
                pet.timeline(float(start), float(end))
        elif op == "mood":
            pet.mood(str(message.get("mood", ""))[:20])
        elif op == "say":
            seconds = message.get("seconds", 6.0)
            pet.say(str(message.get("text", ""))[:200], float(seconds) if isinstance(seconds, (int, float)) else 6.0)
        elif op == "info":
            pet.info(str(message.get("text", ""))[:200])

    def _guard_loop(self) -> None:
        while not self._stop.wait(TICK_SECONDS):
            try:
                self.tick()
            except Exception:
                logger.exception("Hands: bouncer pass failed")

    def tick(self) -> None:
        if not self.armed.is_set() or self.desk.guard is None:
            return
        if time.time() > self.until + GRACE_SECONDS:  # the brain never said stop: don't guard forever
            logger.info("Hands: the round ended %d s ago without word from the brain; standing down", GRACE_SECONDS)
            self.armed.clear()
            self.desk.pet.stop()
            return
        self.desk.guard.tick()


def _beep() -> None:
    try:
        import winsound

        winsound.MessageBeep(winsound.MB_ICONASTERISK)
    except Exception:
        pass


# ---------------------------------------------------------------------- setup from .env
def tunnel_from_env() -> SshTunnel | None:
    target = os.getenv("MIKI_SERVER", "").strip()
    if not target:
        return None
    ssh_port = env_port("MIKI_SSH_PORT", 0) if os.getenv("MIKI_SSH_PORT") else None
    return SshTunnel(
        target, local_port=env_port("MIKI_LINK_LOCAL_PORT", DEFAULT_LOCAL_PORT),
        remote_port=env_port("MIKI_LINK_PORT", DEFAULT_SERVER_PORT), ssh_port=ssh_port,
        key=os.getenv("MIKI_SSH_KEY", "").strip() or None,
    )


def link_address() -> tuple[str, int]:
    """Where this laptop reaches the brain: the local end of the SSH tunnel (or MIKI_LINK_ADDRESS, for testing)."""
    direct = os.getenv("MIKI_LINK_ADDRESS", "").strip()
    if direct:
        host, _, port = direct.rpartition(":")
        return host or "127.0.0.1", int(port)
    return "127.0.0.1", env_port("MIKI_LINK_LOCAL_PORT", DEFAULT_LOCAL_PORT)


def launch_command() -> list[str]:
    """How to start this agent as its own hidden process (from source, or from the packaged Miki.exe)."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--hands"]
    python = Path(sys.executable)
    pythonw = python.with_name("pythonw.exe")
    return [str(pythonw if pythonw.exists() else python), "-m", "app.hands"]


def _startup_dir() -> Path:
    return Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def install_autostart() -> Path:
    project = Path(__file__).resolve().parent.parent.parent
    command = " ".join(f'""{part}""' for part in launch_command())
    script = (
        'Set shell = CreateObject("WScript.Shell")\n'
        f'shell.CurrentDirectory = "{project}"\n'
        f'shell.Run "{command}", 0, False\n'
    )
    target = _startup_dir() / STARTUP_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(script, encoding="utf-8")
    return target


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if "--install-autostart" in args:
        print(f"Miki's hands will start when you log in.\nLauncher: {install_autostart()}")
        return 0
    if "--remove-autostart" in args:
        target = _startup_dir() / STARTUP_FILE
        existed = target.exists()
        target.unlink(missing_ok=True)
        print("Removed the startup launcher." if existed else "There was no startup launcher.")
        return 0

    Path("data").mkdir(exist_ok=True)
    logging.basicConfig(filename="data/hands.log", level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    token = link_token()
    if not token:
        print("Set MIKI_LINK_TOKEN in .env (the same value as on the server). Make one with: python -m app.link token")
        return 1
    if not os.getenv("MIKI_SERVER", "").strip() and not os.getenv("MIKI_LINK_ADDRESS", "").strip():
        print("Set MIKI_SERVER=miki@<server-ip> in .env so I know where Miki's brain lives.")
        return 1
    if not single_instance.acquire_named(LOCK_NAME):
        print("Miki's hands are already running.")
        return 0

    tunnel = tunnel_from_env()
    agent = HandsAgent()
    link = LinkClient(
        link_address(), token, ["hands"], on_message=agent.on_message, state=agent.state,
        before_connect=tunnel.ensure if tunnel is not None else (lambda: None),
        on_status=lambda up, why: logger.info("Link %s%s", "up" if up else "down", f": {why}" if why else ""),
    )
    agent.start(link)
    print(f"Miki's hands are running (brain: {os.getenv('MIKI_SERVER') or os.getenv('MIKI_LINK_ADDRESS')}). Ctrl+C to stop.")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        agent.stop()
        if tunnel is not None:
            tunnel.close()
    return 0
