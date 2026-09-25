"""Proactive pushes to the phone: urgent mail now, more later.

Notifications are the part that can go wrong by being *annoying*, so this is deliberately conservative:
only owners are messaged, each thing at most once, never during quiet hours (it waits until they end),
never more than a few per hour, and never a flood the first time it is switched on.
"""

from __future__ import annotations

import html
import logging
import time
from collections import deque
from datetime import datetime, time as dtime
from typing import Any, Callable

from app.core.mail_triage import MailItem, gmail_url
from app.phone.state import PhoneState
from app.phone.telegram_api import TelegramApi, TelegramError

logger = logging.getLogger(__name__)

DEFAULT_QUIET_HOURS = "23:00-08:00"
MAX_PER_HOUR = 6


def parse_quiet_hours(spec: str) -> tuple[dtime, dtime] | None:
    """'23:00-08:00' -> (23:00, 08:00). 'off' / '' / garbage -> None (no quiet hours)."""
    spec = (spec or "").strip().lower()
    if spec in {"", "off", "none", "no", "0"}:
        return None
    try:
        start, end = (part.strip() for part in spec.split("-", 1))
        return dtime.fromisoformat(start), dtime.fromisoformat(end)
    except ValueError:
        logger.warning("Ignoring invalid MIKI_QUIET_HOURS=%r (expected e.g. 23:00-08:00)", spec)
        return None


def in_quiet_hours(now: datetime, spec: str) -> bool:
    window = parse_quiet_hours(spec)
    if window is None:
        return False
    start, end = window
    current = now.time().replace(second=0, microsecond=0)
    if start == end:
        return False
    return start <= current < end if start < end else (current >= start or current < end)  # overnight window


class PhoneNotifier:
    def __init__(
        self,
        api: TelegramApi,
        state: PhoneState,
        *,
        quiet_hours: str = DEFAULT_QUIET_HOURS,
        max_per_hour: int = MAX_PER_HOUR,
        now: Callable[[], datetime] = datetime.now,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.api = api
        self.state = state
        self.quiet_hours = quiet_hours
        self.max_per_hour = max_per_hour
        self._now = now
        self._clock = clock
        self._sent: deque[float] = deque()

    # ------------------------------------------------------------------ core
    def send(self, text: str, *, key: str | None = None, buttons: list[list[dict[str, str]]] | None = None) -> bool:
        """Send one push if allowed. Returns True only if it was actually delivered."""
        chat_id = self.state.chat_id
        if chat_id is None:
            return False
        if key is not None and self.state.already_notified(key):
            return False
        if in_quiet_hours(self._now(), self.state.pref("quiet_hours", self.quiet_hours)):
            return False  # not marked as notified, so it goes out once quiet hours end
        cutoff = self._clock() - 3600
        while self._sent and self._sent[0] < cutoff:
            self._sent.popleft()
        if len(self._sent) >= self.max_per_hour:
            return False
        try:
            self.api.send_message(chat_id, text, buttons=buttons)
        except TelegramError as exc:
            logger.warning("Push failed: %s", exc.description)
            return False
        self._sent.append(self._clock())
        if key is not None:
            self.state.mark_notified(key)
        return True

    # ------------------------------------------------------------------ mail
    def notify_mail(self, items: list[MailItem], account: str = "") -> int:
        """Push each *urgent* mail once. The first call only records what's already there (no flood)."""
        urgent = [item for item in items if item.priority == "urgent"]
        if not self.state.pref("urgent_push"):
            return 0
        if not self.state.flag("mail_baselined"):
            for item in urgent:
                self.state.mark_notified(f"mail:{item.id}")
            self.state.set_flag("mail_baselined")
            return 0

        sent = 0
        for item in urgent:
            if self.send(self._mail_text(item), key=f"mail:{item.id}", buttons=self._mail_buttons(item, account)):
                sent += 1
        return sent

    @staticmethod
    def _mail_text(item: MailItem) -> str:
        detail = item.reason or item.snippet[:100]
        if item.due:
            detail += f" · due {item.due}"
        return (
            f"<b>URGENT</b> · {html.escape(item.sender_name[:40])}\n"
            f"{html.escape(item.subject[:120])}\n"
            f"<i>{html.escape(detail[:200])}</i>"
        )

    @staticmethod
    def _mail_buttons(item: MailItem, account: str) -> list[list[dict[str, Any]]]:
        return [[
            {"text": "📨 Open in Gmail", "url": gmail_url(item.thread_id, account)},
            {"text": "✓ Dismiss", "callback_data": f"md:{item.id}"},
            {"text": "💤 Snooze", "callback_data": f"mz:{item.id}"},
        ]]
