"""Which emails actually need the user's attention?

A dashboard that lists the latest unread mail is a to-do list nobody asked for. This module decides what
deserves attention, and why, so the widget can show three rows instead of thirty:

1. cheap rules first: bulk mail (newsletters, promotions) is dropped without spending a token unless it
   contains a genuine-deadline style keyword;
2. one batched AI call classifies the rest as urgent / important / fyi / ignore, with a short reason, a
   suggested action and a due date, using what Miki knows about the user (people who matter, studies...);
3. every verdict is cached per message id, so each email is judged exactly once, however often the
   dashboard refreshes.

Dismissals are local: they hide an item in Miki without touching the mailbox.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import quote

logger = logging.getLogger(__name__)

PRIORITIES = ("urgent", "important", "fyi", "ignore")
_RANK = {name: index for index, name in enumerate(PRIORITIES)}
_CACHE_DAYS = 45
_FALLBACK_RETRY_SECONDS = 3600  # a guessed verdict is kept for an hour, then the model gets another chance

# Bulk mail is normally noise, but these words mean "a real deadline or account event" even when a
# no-reply address sends them, so such mail still goes to the model.
_BULK_WORTH_READING = re.compile(
    r"deadline|due (?:date|on|by|today|tomorrow)|action required|payment|invoice|overdue|exam|tentamen|"
    r"security alert|verify|verification|expires?|appointment|delivery|delivered|shipped|password|"
    r"confirm your|cancel|refund|booking|reservation|ticket",
    re.IGNORECASE,
)


class BrainProtocol(Protocol):
    def generate_response(self, user_input: str, system_prompt: str, history: list[dict[str, str]] | None = None) -> str:
        ...


@dataclass
class MailItem:
    id: str
    thread_id: str
    sender_name: str
    sender_email: str
    subject: str
    snippet: str
    date: str
    priority: str
    reason: str
    action: str = "none"
    due: str = ""
    needs_reply: bool = False
    unread: bool = True


def gmail_url(thread_id: str, account_email: str = "") -> str:
    """A link that opens this exact conversation in Gmail (in the right account if it is known)."""
    account = quote(account_email, safe="@") if account_email else "0"
    return f"https://mail.google.com/mail/u/{account}/#all/{thread_id}"


_SYSTEM_PROMPT = """You triage one person's Gmail inbox so a personal assistant can show only the mail that \
needs their attention. Decide for each email using only what is shown; never invent details.

Priorities:
- "urgent": time-sensitive and needs action soon: a deadline within about 3 days, someone waiting on a reply, a \
meeting or booking changing today/tomorrow, a security or payment problem.
- "important": needs action or a decision but not immediately, or a personal message from a real person, or \
school/work/finance/health/delivery mail that matters.
- "fyi": informational, no action needed.
- "ignore": promotions, newsletters, automated notifications and social updates with no action.

Rules:
- A real person writing to the user personally is at least "important", especially anyone in the people list.
- "reason": at most 12 words, concrete ("Professor asks for your answer by Friday"), never generic.
- "action": one of reply, pay, read, attend, confirm, sign, none.
- "due": a YYYY-MM-DD date only if the email states or clearly implies one, else "".
- "needs_reply": true only if a person is waiting for an answer.

Respond ONLY with valid JSON (no markdown):
{"emails": [{"id": "...", "priority": "urgent|important|fyi|ignore", "reason": "...", "action": "...", "due": "", "needs_reply": false}]}"""


class MailTriage:
    def __init__(
        self,
        brain: BrainProtocol | None,
        *,
        cache_path: str | Path = "data/mail_triage.json",
        dismissed_path: str | Path = "data/mail_dismissed.json",
        snoozed_path: str | Path | None = None,
        context: Callable[[], str] | None = None,
        batch_size: int = 20,
    ) -> None:
        self.brain = brain
        self.cache_path = Path(cache_path)
        self.dismissed_path = Path(dismissed_path)
        self.snoozed_path = Path(snoozed_path) if snoozed_path else self.dismissed_path.with_name(self.dismissed_path.stem.replace("dismissed", "snoozed") + ".json")
        self.context = context
        self.batch_size = batch_size

    # ------------------------------------------------------------------ persistence
    def _load(self, path: Path, default: Any) -> Any:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, type(default)) else default
        except (OSError, ValueError):
            return default

    def _save(self, path: Path, data: Any) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except OSError:
            logger.warning("Could not save %s", path, exc_info=True)

    def dismissed(self) -> set[str]:
        return set(self._load(self.dismissed_path, []))

    def dismiss(self, message_id: str) -> None:
        ids = self.dismissed()
        ids.add(message_id)
        self._save(self.dismissed_path, sorted(ids))

    def restore_all(self) -> None:
        self._save(self.dismissed_path, [])
        self._save(self.snoozed_path, {})

    def snooze(self, message_id: str, hours: float = 20.0) -> None:
        """Hide an item for a while (default: until roughly tomorrow), then let it come back."""
        snoozed = self._load(self.snoozed_path, {})
        snoozed[message_id] = time.time() + hours * 3600
        self._save(self.snoozed_path, snoozed)

    def snoozed(self) -> set[str]:
        """Ids still snoozed right now (expired ones are forgotten)."""
        now = time.time()
        snoozed = self._load(self.snoozed_path, {})
        active = {k: v for k, v in snoozed.items() if isinstance(v, (int, float)) and v > now}
        if len(active) != len(snoozed):
            self._save(self.snoozed_path, active)
        return set(active)

    # ------------------------------------------------------------------ triage
    def triage(self, emails: list[dict[str, Any]]) -> list[MailItem]:
        """A verdict for every email: cached ones instantly, new ones with a single batched model call."""
        cache: dict[str, dict[str, Any]] = self._load(self.cache_path, {})
        verdicts: dict[str, dict[str, Any]] = {}
        to_ask: list[dict[str, Any]] = []

        for email in emails:
            message_id = email["id"]
            cached = cache.get(message_id)
            if cached is not None and not self._expired_guess(cached):
                verdicts[message_id] = cached
                continue
            rule = self._rule_verdict(email)
            if rule is not None:
                verdicts[message_id] = cache[message_id] = rule
            else:
                to_ask.append(email)

        if to_ask:
            for start in range(0, len(to_ask), self.batch_size):
                chunk = to_ask[start : start + self.batch_size]
                answered = self._ask_model(chunk)
                for email in chunk:
                    message_id = email["id"]
                    if message_id in answered:
                        verdicts[message_id] = cache[message_id] = answered[message_id]
                    else:  # the model skipped/failed this one: remember a stop-gap guess, but retry it later
                        verdicts[message_id] = cache[message_id] = self._fallback_verdict(email)

        self._prune_and_save(cache)
        return [self._item(email, verdicts[email["id"]]) for email in emails]

    def attention(self, emails: list[dict[str, Any]], limit: int = 6) -> list[MailItem]:
        """Only what needs attention (urgent first, then important, newest first), minus dismissed."""
        hidden = self.dismissed() | self.snoozed()
        items = [i for i in self.triage(emails) if i.priority in {"urgent", "important"} and i.id not in hidden]
        items.sort(key=lambda i: i.date, reverse=True)  # newest first...
        items.sort(key=lambda i: _RANK[i.priority])  # ...then urgent before important (stable)
        return self._collapse_duplicates(items)[:limit]

    @staticmethod
    def _collapse_duplicates(items: list[MailItem]) -> list[MailItem]:
        """The same alert sent twice (same sender and subject) is one row, not two."""
        seen: dict[tuple[str, str], MailItem] = {}
        extra: dict[tuple[str, str], int] = {}
        ordered: list[MailItem] = []
        for item in items:
            key = (item.sender_email.lower(), item.subject.strip().lower())
            if key in seen:
                extra[key] = extra.get(key, 0) + 1
                continue
            seen[key] = item
            ordered.append(item)
        for key, count in extra.items():
            item = seen[key]
            item.reason = (f"{item.reason} (+{count} similar)" if item.reason else f"+{count} similar")
        return ordered

    # ------------------------------------------------------------------ internals
    @staticmethod
    def _rule_verdict(email: dict[str, Any]) -> dict[str, Any] | None:
        """Free, deterministic verdicts for clear-cut noise (None = ask the model)."""
        gmail_important = email.get("important") or email.get("starred")
        if email.get("bulk") and not gmail_important:
            text = f"{email.get('subject', '')} {email.get('snippet', '')}"
            if not _BULK_WORTH_READING.search(text):
                return {"priority": "ignore", "reason": "Bulk or automated mail", "action": "none", "due": "", "needs_reply": False, "ts": time.time()}
        return None

    def _ask_model(self, emails: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        if self.brain is None:
            return {}
        context = ""
        try:
            context = (self.context() if self.context else "") or ""
        except Exception:
            logger.debug("Mail context unavailable", exc_info=True)
        rows = [
            {
                "id": e["id"],
                "from": f"{e.get('sender_name', '')} <{e.get('sender_email', '')}>",
                "subject": str(e.get("subject", ""))[:160],
                "snippet": str(e.get("snippet", ""))[:240],
                "date": e.get("date", ""),
                "unread": bool(e.get("unread")),
                "gmail_marked_important": bool(e.get("important") or e.get("starred")),
                "automated_or_bulk": bool(e.get("bulk")),
            }
            for e in emails
        ]
        prompt = (
            f"Today is {date.today().isoformat()}.\n"
            + (f"About the user: {context}\n" if context else "")
            + f"\nEmails:\n{json.dumps(rows, ensure_ascii=False)}\n\nTriage them now and respond with the JSON object only."
        )
        try:
            text = self.brain.generate_response(user_input=prompt, system_prompt=_SYSTEM_PROMPT, history=None)
            payload = _parse_json(text)
        except Exception:
            logger.exception("Mail triage call failed")
            return {}

        answered: dict[str, dict[str, Any]] = {}
        wanted = {e["id"] for e in emails}
        for item in payload.get("emails", []) if isinstance(payload, dict) else []:
            if not isinstance(item, dict) or item.get("id") not in wanted:
                continue
            priority = str(item.get("priority", "")).strip().lower()
            if priority not in _RANK:
                continue
            answered[item["id"]] = {
                "priority": priority,
                "reason": str(item.get("reason", "")).strip()[:140],
                "action": str(item.get("action", "none")).strip().lower()[:12] or "none",
                "due": _clean_date(item.get("due")),
                "needs_reply": bool(item.get("needs_reply", False)),
                "ts": time.time(),
            }
        return answered

    @staticmethod
    def _expired_guess(verdict: dict[str, Any]) -> bool:
        return bool(verdict.get("guess")) and time.time() - float(verdict.get("ts", 0)) > _FALLBACK_RETRY_SECONDS

    @staticmethod
    def _fallback_verdict(email: dict[str, Any]) -> dict[str, Any]:
        base = {"action": "none", "due": "", "needs_reply": False, "guess": True, "ts": time.time()}
        if email.get("important") or email.get("starred"):
            return {**base, "priority": "important", "reason": "Marked important in Gmail", "action": "read"}
        return {**base, "priority": "fyi", "reason": ""}

    @staticmethod
    def _item(email: dict[str, Any], verdict: dict[str, Any]) -> MailItem:
        return MailItem(
            id=email["id"],
            thread_id=email.get("thread_id", email["id"]),
            sender_name=email.get("sender_name") or email.get("sender", ""),
            sender_email=email.get("sender_email", ""),
            subject=email.get("subject", ""),
            snippet=email.get("snippet", ""),
            date=email.get("date", ""),
            priority=verdict["priority"],
            reason=verdict.get("reason", ""),
            action=verdict.get("action", "none"),
            due=verdict.get("due", ""),
            needs_reply=bool(verdict.get("needs_reply", False)),
            unread=bool(email.get("unread", True)),
        )

    def _prune_and_save(self, cache: dict[str, dict[str, Any]]) -> None:
        cutoff = time.time() - _CACHE_DAYS * 86400
        fresh = {k: v for k, v in cache.items() if float(v.get("ts", time.time())) >= cutoff}
        self._save(self.cache_path, fresh)


def _clean_date(value: Any) -> str:
    text = str(value or "").strip()
    return text if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text) else ""


def _parse_json(text: str) -> dict:
    cleaned = (text or "").strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    if cleaned.startswith("```"):
        cleaned = cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    return json.loads(cleaned.strip())
