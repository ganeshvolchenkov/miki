"""Run Miki with no window: on the server, this is Miki's always-on brain.

    python -m app.headless                       run in the foreground (Ctrl+C to stop)
    python -m app.headless --install-autostart   start it silently when you log in to Windows
    python -m app.headless --remove-autostart

What it becomes depends on .env:
    MIKI_LINK_TOKEN set (the server)   the brain: phone bot + dashboard back end + focus clock, laptops connect in
    MIKI_SERVER set (the laptop)       the hands agent instead (never a second brain)
    neither                            just the phone bot, as before

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
    from dotenv import load_dotenv

    load_dotenv()  # before anything below reads MIKI_SERVER / MIKI_LINK_TOKEN
    _use_configured_timezone()

    from app.link.protocol import link_token

    if os.getenv("MIKI_SERVER", "").strip():
        # This machine is the laptop: the brain lives on the server. Never start a second brain (a second memory, a
        # second bot) here; be the hands instead. That also turns an older "Miki Phone" login launcher into the hands.
        from app.hands.agent import main as hands_main

        return hands_main([])
    if link_token():
        return run_brain()

    from app.core.bootstrap import build_runtime
    from app.focus.service import build_focus_service
    from app.phone.service import build_phone_service
    from app.plan.service import build_plan_service

    runtime = build_runtime()
    focus = build_focus_service(core=runtime.core)
    plan = build_plan_service(runtime.core, focus=focus)
    phone = build_phone_service(runtime.core, runtime.settings, openai_client=runtime.openai_client, focus=focus, plan=plan)
    if phone is None:
        print("No Telegram bot is configured. Set TELEGRAM_BOT_TOKEN in .env first (see /phone in the Miki dashboard).")
        return 1
    if not phone.start():
        print("The phone bot is already running in another Miki process. Nothing to do.")
        return 0

    if focus is not None:
        focus.start()  # the focus clock lives with the bot: /focus from your phone is handled here
    if plan is not None:
        plan.start()  # and so does the day plan's: its nudges go to the phone
    time.sleep(2)  # give the bot a moment to look itself up
    status = phone.status()
    print(f"Miki phone service is running as @{status.username or '?'} ({'linked' if status.paired else 'not linked yet'}). Ctrl+C to stop.")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        phone.stop()
        if focus is not None:
            focus.close()
        if plan is not None:
            plan.close()
    return 0


def _use_configured_timezone() -> None:
    """Servers usually run on UTC. Focus times, the recap, quiet hours and the morning brief all read the local clock,
    so on Linux/macOS make MIKI_TIMEZONE (e.g. Europe/Amsterdam) this process's local time."""
    zone = os.getenv("MIKI_TIMEZONE", "").strip()
    if not zone or not hasattr(time, "tzset"):
        return
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(zone)
    except Exception:
        logging.getLogger(__name__).warning("MIKI_TIMEZONE=%r isn't a timezone I know; keeping the system's", zone)
        return
    os.environ["TZ"] = zone
    time.tzset()


def run_brain() -> int:
    """Miki's brain, always on (the server): the phone bot, the dashboard's back end and the focus clock in one process.

    The laptop connects through the link (see ``app.link``): its dashboard window shows what happens here, and its
    hands agent arranges the screens and runs the bouncer when a focus round starts.
    """
    from app.core.bootstrap import build_runtime
    from app.core.model_choice import load_saved_model
    from app.focus.service import build_focus_service
    from app.interfaces.web_gui import WebApi
    from app.link.hub import LinkHub
    from app.link.protocol import DEFAULT_SERVER_PORT, env_port, link_token
    from app.link.remote import RemoteHands, RemoteWindow, wire_dashboard, wire_focus
    from app.phone.service import build_phone_service
    from app.plan.service import build_plan_service

    runtime = build_runtime(saved_model=load_saved_model())
    hub = LinkHub(link_token(), host="127.0.0.1", port=env_port("MIKI_LINK_PORT", DEFAULT_SERVER_PORT))
    api = WebApi(runtime.core, runtime.brain)
    api._window = RemoteWindow(hub)
    api._focus = build_focus_service(core=runtime.core, hands=RemoteHands(hub))
    api._plan = build_plan_service(runtime.core, focus=api._focus)
    api._phone = build_phone_service(
        runtime.core, runtime.settings, openai_client=runtime.openai_client, lock=api._core_lock,
        hooks=api.phone_hooks(), focus=api._focus, plan=api._plan,
    )
    if api._focus is not None:
        wire_focus(hub, api._focus)
    wire_dashboard(hub, api)
    hub.start()
    api.start_loops()  # memory upkeep, the focus clock, the phone bot

    phone = api._phone.status() if api._phone is not None else None
    print(
        f"Miki's brain is running. Link: 127.0.0.1:{hub.port} (reach it through SSH). "
        + (f"Phone: @{phone.username or '?'} ({'linked' if phone.paired else 'not linked yet'})." if phone else "Phone: not configured.")
        + " Ctrl+C to stop."
    )
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        hub.stop()
        if api._phone is not None:
            api._phone.stop()
        if api._focus is not None:
            api._focus.close()
        if api._plan is not None:
            api._plan.close()
    return 0


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    os._exit(code)
