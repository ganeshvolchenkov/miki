"""Run Miki's phone service with no window (for an always-on machine or a background launch).

    python -m app.headless                       run in the foreground (Ctrl+C to stop)
    python -m app.headless --install-autostart   start it silently when you log in to Windows
    python -m app.headless --remove-autostart

Only one process can run the bot at a time: if the dashboard (or another headless copy) already runs it,
this exits politely.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

STARTUP_FILE = "Miki Phone.vbs"


def _startup_dir() -> Path:
    return Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def install_autostart() -> Path:
    """Drop a tiny launcher in the Windows Startup folder that runs this module hidden, from the project folder."""
    project = Path(__file__).resolve().parent.parent
    python = Path(sys.executable)
    pythonw = python.with_name("pythonw.exe")
    runner = pythonw if pythonw.exists() else python
    script = (
        'Set shell = CreateObject("WScript.Shell")\n'
        f'shell.CurrentDirectory = "{project}"\n'
        f'shell.Run """{runner}"" -m app.headless", 0, False\n'
    )
    target = _startup_dir() / STARTUP_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(script, encoding="utf-8")
    return target


def remove_autostart() -> bool:
    target = _startup_dir() / STARTUP_FILE
    if target.exists():
        target.unlink()
        return True
    return False


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if "--install-autostart" in args:
        print(f"Miki's phone service will start when you log in.\nLauncher: {install_autostart()}")
        return 0
    if "--remove-autostart" in args:
        print("Removed the startup launcher." if remove_autostart() else "There was no startup launcher.")
        return 0

    Path("data").mkdir(exist_ok=True)
    logging.basicConfig(filename="data/phone.log", level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    from app.core.bootstrap import build_runtime
    from app.phone.service import build_phone_service

    runtime = build_runtime()
    phone = build_phone_service(runtime.core, runtime.settings, openai_client=runtime.openai_client)
    if phone is None:
        print("No Telegram bot is configured. Set TELEGRAM_BOT_TOKEN in .env first (see /phone in the Miki dashboard).")
        return 1
    if not phone.start():
        print("The phone bot is already running in another Miki process. Nothing to do.")
        return 0

    time.sleep(2)  # give the bot a moment to look itself up
    status = phone.status()
    print(f"Miki phone service is running as @{status.username or '?'} ({'linked' if status.paired else 'not linked yet'}). Ctrl+C to stop.")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        phone.stop()
    return 0


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    os._exit(code)
