"""Puts the phone pieces together and runs them in the background."""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from app.core import single_instance
from app.core.config import Settings
from app.core.mail_attention import MailAttention
from app.core.mail_triage import MailTriage
from app.phone.backend import CoreBackend, PhoneHooks
from app.phone.bot import PhoneBot
from app.phone.notifier import PhoneNotifier
from app.phone.state import PhoneState
from app.phone.telegram_api import TelegramApi

logger = logging.getLogger(__name__)

BOT_LOCK_NAME = "MikiPhoneBot"
DEFAULT_WATCH_SECONDS = 300


@dataclass
class PhoneStatus:
    configured: bool
    owns_bot: bool = False
    running: bool = False
    paired: bool = False
    username: str = ""
    owner: str = ""
    error: str = ""


class PhoneService:
    def __init__(
        self,
        core: Any,
        settings: Settings,
        *,
        openai_client: Any = None,
        lock: threading.RLock | None = None,
        hooks: PhoneHooks | None = None,
        api: TelegramApi | None = None,
        state: PhoneState | None = None,
        watch_seconds: float = DEFAULT_WATCH_SECONDS,
    ) -> None:
        self.core = core
        self.hooks = hooks or PhoneHooks()
        self.api = api or TelegramApi(settings.telegram_bot_token or "")
        self.state = state or PhoneState()
        manager = getattr(core, "memory_manager", None)
        triage = MailTriage(getattr(core, "brain", None), context=manager.mail_context if manager is not None else None)
        self.mail = MailAttention(lambda name: core.get_tool(name), lambda: triage)
        self.backend = CoreBackend(
            core, lock=lock, mail=self.mail, openai_client=openai_client, transcribe_model=settings.transcribe_model,
            tts_model=settings.tts_model, tts_voice=settings.tts_voice, hooks=self.hooks,
        )
        self.bot = PhoneBot(self.api, self.state, self.backend, hooks=self.hooks, quiet_default=settings.quiet_hours)
        self.notifier = PhoneNotifier(self.api, self.state, quiet_hours=settings.quiet_hours)
        self._watch_seconds = float(os.getenv("MIKI_MAIL_WATCH_SECONDS", "") or watch_seconds)
        self._stop = threading.Event()
        self._owns_bot = False

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> bool:
        """Start the bot and the mail watcher, unless another Miki process already runs the bot."""
        if not single_instance.acquire_named(BOT_LOCK_NAME):
            logger.info("The phone bot is already running in another Miki process")
            return False
        self._owns_bot = True
        self._stop.clear()
        self.bot.start()
        threading.Thread(target=self._watch_mail, name="miki-mail-watch", daemon=True).start()
        return True

    def stop(self) -> None:
        self._stop.set()
        self.bot.stop()

    def _watch_mail(self) -> None:
        """Background upkeep: every few minutes refresh the attention list and push anything newly urgent;
        every minute check whether it is time for the (opt-in) morning brief."""
        self._stop.wait(20)  # let startup settle
        last_mail: float | None = None
        while not self._stop.is_set():
            if self.state.is_paired:
                now = time.monotonic()
                if last_mail is None or now - last_mail >= self._watch_seconds:
                    last_mail = now
                    try:
                        items = self.mail.refresh(force=True)
                        if items is not None:
                            self.notifier.notify_mail(items, self.mail.account)
                    except Exception:
                        logger.debug("Mail watch cycle failed", exc_info=True)
                self.send_brief_if_due()
            self._stop.wait(min(60.0, self._watch_seconds))

    def send_brief_if_due(self, now: datetime | None = None) -> bool:
        """Push the morning brief once a day, from the chosen time until 3 hours later (never a stale one)."""
        if not self.state.pref("morning_brief"):
            return False
        now = now or datetime.now()
        try:
            hour, minute = (int(part) for part in str(self.state.pref("brief_time")).split(":"))
            due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        except ValueError:
            return False
        if not due <= now <= due + timedelta(hours=3):
            return False
        try:
            screen = self.bot._brief_screen()
        except Exception:
            logger.debug("Could not build the morning brief", exc_info=True)
            return False
        return self.notifier.send(screen.text, key=f"brief:{now.date().isoformat()}", buttons=screen.buttons)

    # ------------------------------------------------------------------ dashboard helpers
    def status(self) -> PhoneStatus:
        return PhoneStatus(
            configured=True,
            owns_bot=self._owns_bot,
            running=self.bot.running,
            paired=self.state.is_paired,
            username=self.bot.username,
            owner=self.state.owner_name,
            error=self.bot.last_error,
        )

    def new_pairing(self) -> tuple[str, str]:
        """(one-time code, one-tap link). Sending either to the bot links that account as the owner."""
        code = self.state.new_pairing_code()
        link = f"https://t.me/{self.bot.username}?start={code}" if self.bot.username else ""
        return code, link

    def send_test(self) -> bool:
        return self.notifier.send("Test from Miki. If you can read this, your phone is linked.", key=None)

    def unlink(self) -> None:
        self.state.unpair()


def build_phone_service(core: Any, settings: Settings, **kwargs: Any) -> PhoneService | None:
    """The phone service, or None if no bot token is configured (or MIKI_PHONE=0)."""
    if not settings.telegram_bot_token or os.getenv("MIKI_PHONE", "1").strip().lower() in {"0", "false", "no"}:
        return None
    try:
        return PhoneService(core, settings, **kwargs)
    except ValueError as exc:  # malformed token
        logger.warning("Phone disabled: %s", exc)
        return None
