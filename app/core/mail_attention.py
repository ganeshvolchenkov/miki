"""The "mail that needs attention" service, shared by the dashboard and the phone.

Fetches recent candidate mail (metadata only), triages it, and keeps the latest result. A minimum interval
protects Gmail from being hit on every chat message or refresh.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

from app.core.mail_triage import MailItem, MailTriage

logger = logging.getLogger(__name__)

QUERY = (
    "in:inbox newer_than:14d (is:unread OR is:starred OR is:important) "
    "-category:promotions -category:social -category:forums"
)


class MailUnavailable(Exception):
    """Mail isn't connected or the client can't do triage."""


class MailAttention:
    def __init__(
        self,
        get_tool: Callable[[str], Any],
        get_triage: Callable[[], MailTriage],
        *,
        min_interval: float = 180.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._get_tool = get_tool
        self._get_triage = get_triage
        self._min_interval = min_interval
        self._clock = clock
        self.items: list[MailItem] = []
        self.account = ""
        self.checked = 0.0

    def refresh(self, force: bool = False) -> list[MailItem] | None:
        """The current attention list, or ``None`` if it was refreshed too recently (unchanged)."""
        if not force and self._clock() - self.checked < self._min_interval:
            return None
        tool = self._get_tool("mail")
        if tool is None:
            raise MailUnavailable("Mail isn't connected.")
        client = tool.get_client()
        fetch = getattr(client, "triage_candidates", None)
        if fetch is None:
            raise MailUnavailable("This mail client can't do triage.")

        emails = fetch(query=QUERY, max_results=25)
        try:
            self.account = client.profile_email()
        except Exception:
            logger.debug("Could not read the Gmail account address", exc_info=True)
        self.items = self._get_triage().attention(emails, limit=50)
        self.checked = self._clock()
        return self.items

    def dismiss(self, message_id: str) -> None:
        self._get_triage().dismiss(message_id)
        self.items = [item for item in self.items if item.id != message_id]

    def snooze(self, message_id: str, hours: float = 20.0) -> None:
        self._get_triage().snooze(message_id, hours)
        self.items = [item for item in self.items if item.id != message_id]

    def summary(self) -> str:
        """The same text the dashboard's /mail prints (also used on the phone)."""
        if not self.items:
            return "Nothing in your inbox needs attention right now."
        lines = [f"{len(self.items)} email{'s' if len(self.items) != 1 else ''} need your attention", ""]
        for item in self.items:
            due = f" (due {item.due})" if item.due else ""
            lines.append(f"{item.priority.upper()} · {item.sender_name[:24]} — {item.subject[:50]}")
            lines.append(f"    {item.reason or item.snippet[:70]}{due}")
        return "\n".join(lines)
