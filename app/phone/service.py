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
from app.inbox.service import build_inbox_service
from app.phone.backend import CoreBackend, PhoneHooks
from app.phone.bot import PhoneBot
from app.phone import ui
from app.phone.notifier import PhoneNotifier
from app.phone.state import PhoneState
from app.phone.telegram_api import TelegramApi

logger = logging.getLogger(__name__)

BOT_LOCK_NAME = "MikiPhoneBot"
DEFAULT_WATCH_SECONDS = 300
DEFAULT_CLEANUP_HOURS = 24.0  # the chat with Miki keeps a day of messages; older ones are deleted
PURGE_BATCH = 50


def _hours(value: str | None, default: float) -> float:
    try:
        return max(0.0, float(value)) if value not in (None, "") else default
    except ValueError:
        return default


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
        focus: Any = None,
        plan: Any = None,
    ) -> None:
        self.core = core
        self.focus = focus
        self.plan = plan
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
        self.inbox = build_inbox_service(core, openai_client)  # reads new mail and photos, acts on what's in them
        self.bot = PhoneBot(self.api, self.state, self.backend, hooks=self.hooks, quiet_default=settings.quiet_hours, focus=focus, plan=plan,
                            inbox=self.inbox)
        self.notifier = PhoneNotifier(
            self.api, self.state, quiet_hours=settings.quiet_hours, hold=(lambda: focus.is_focusing) if focus is not None else (lambda: False)
        )
        if focus is not None:
            focus.add_listener(self._focus_event)
        if plan is not None:
            plan.add_listener(self._plan_nudge)
        if self.inbox is not None:
            self.inbox.add_listener(self._inbox_notice)
        self._watch_seconds = float(os.getenv("MIKI_MAIL_WATCH_SECONDS", "") or watch_seconds)
        self.cleanup_hours = _hours(os.getenv("MIKI_PHONE_CLEANUP_HOURS"), DEFAULT_CLEANUP_HOURS)  # 0 = never delete anything
        self.api.on_sent = self.state.track_message
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
                            self._read_new_mail(items)
                    except Exception:
                        logger.debug("Mail watch cycle failed", exc_info=True)
                self.send_brief_if_due()
                self.purge_old_messages()
                if self.inbox is not None:
                    try:
                        self.inbox.tick()
                    except Exception:
                        logger.exception("Inbox tick failed")
            self._stop.wait(min(60.0, self._watch_seconds))

    def purge_old_messages(self) -> int:
        """Delete messages (yours and Miki's) once they are ``cleanup_hours`` old, so the chat doesn't pile up.

        Telegram only lets a bot delete messages younger than 48 hours; anything older it refuses, and is dropped from the
        log anyway (nothing to retry). Only messages this bot saw or sent are touched, never other chats. Returns how many went."""
        if self.cleanup_hours <= 0 or not self.state.is_paired:
            return 0
        old = self.state.messages_older_than(self.cleanup_hours * 3600, PURGE_BATCH)
        deleted = 0
        for chat_id, message_id in old:
            if self.api.delete_message(chat_id, message_id):
                deleted += 1
        if old:
            self.state.forget_messages(set(old))
        return deleted

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

    def _focus_event(self, event: Any) -> None:
        """Focus timers (10 minutes left, break time, break over) go to the phone at once, whatever the quiet hours say."""
        summary = event.summary or {}
        ask_goal = bool(summary.get("goal")) and summary.get("goal_done") is None
        screen = ui.focus_event_screen(event.kind, event.text, self.focus.config.minutes, ask_goal=event.kind == "time_up" and ask_goal)
        self.notifier.send(screen.text, key=None, buttons=screen.buttons, direct=True)

    def _read_new_mail(self, items: list[Any]) -> None:
        """New important mail: Miki reads it, adds its dates to the calendar and texts you what to do."""
        if self.inbox is None:
            return
        tool = self.core.get_tool("mail") if hasattr(self.core, "get_tool") else None
        client = tool.get_client() if tool is not None else None
        fetch = getattr(client, "get_body", None)
        self.inbox.process_mail(items, fetch if fetch is not None else (lambda message_id: None))

    def _inbox_notice(self, notice: Any) -> bool:
        """What Miki did from your mail (added to the calendar, something to do, a reminder). It waits out quiet hours."""
        return self.notifier.send(ui.esc(notice.text), key=notice.key, buttons=ui.inbox_buttons(notice.batch, notice.link))

    def _plan_nudge(self, nudge: Any) -> None:
        """Your day plan's nudges (time to leave, a subject starting). You asked for them, so like focus timers they
        go out at once; each one only once."""
        buttons = ui.plan_nudge_buttons(nudge.focus_block, nudge.focus_minutes) if self.focus is not None else None
        self.notifier.send(ui.esc(nudge.text), key=nudge.key, buttons=buttons, direct=True)

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
