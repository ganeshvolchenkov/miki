"""Miki reads for you: new important email and photos you send, then acts on what's in them.

    mail (important or urgent)  ─┐
    a photo you send on Telegram ─┴► the AI reads it (reader.py) ► checked findings (findings.py) ►
        • dates and deadlines go into your Google Calendar by themselves, tagged "Added by Miki" and undoable
        • "sign up / pay / bring…" things become a text to you saying exactly what to do, plus a reminder the day before
        • what's worth knowing is remembered

What it will *not* do, whatever an email says: send or reply to mail, open a link, sign up or pay for anything, change or
delete an event it didn't add. Email text is untrusted: the AI's answer is only data (see ``findings.py``) and the actions
above are fixed in this file. Everything it adds sits in one batch you can undo with one tap.

State lives in ``data/inbox.json``. Runs on the brain; ``tick`` is called every minute or so by the phone watcher.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import Any, Callable

from app.inbox import findings as fd
from app.inbox import reader

logger = logging.getLogger(__name__)

MARKER = "Added by Miki from"  # starts the description of every event Miki adds on its own (see ``_tag``)
MAX_EVENTS_PER_SOURCE = 8
MAX_SEEN = 600
MAX_BATCHES = 60
REMINDER_HOUR = 9  # "due tomorrow" reminders go out at 09:00 the day before
STALE_REMINDER_HOURS = 36
DEFAULT_EVENT_MINUTES = 60
DEADLINE_MINUTES = 30
MAX_OUTBOX = 20


@dataclass
class Notice:
    """Something to tell the owner. ``batch`` lets the message carry an Undo button; ``link`` an "Open" button."""

    text: str
    key: str
    batch: str = ""
    link: str = ""


@dataclass
class InboxReply:
    text: str
    batch: str = ""
    link: str = ""


def _when(finding: fd.Finding) -> str:
    if finding.day is None:
        return ""
    text = finding.day.strftime("%a %d %b").replace(" 0", " ")
    if finding.start is not None:
        text += f" {finding.start // 60:02d}:{finding.start % 60:02d}"
    return text


def _iso(day: date, minute: int) -> str:
    return (datetime.combine(day, dtime.min) + timedelta(minutes=minute)).astimezone().isoformat(timespec="seconds")


class InboxService:
    def __init__(
        self,
        path: str | Path = "data/inbox.json",
        *,
        text_ai: Callable[[str, str], Any] | None = None,
        image_ai: Callable[[bytes, str, str, str], Any] | None = None,
        calendar: Callable[[], Any] | None = None,
        remember: Callable[[str], Any] | None = None,
        about: Callable[[], str] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = Path(path)
        self._text_ai = text_ai
        self._image_ai = image_ai
        self._calendar = calendar
        self._remember = remember
        self._about = about
        self._clock = clock
        self._lock = threading.RLock()
        self._listeners: list[Callable[[Notice], bool]] = []
        self._data = self._load()

    # ------------------------------------------------------------------ storage
    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            logger.warning("inbox.json is unreadable; keeping it as inbox.corrupt and starting fresh")
            try:
                self.path.replace(self.path.with_suffix(".corrupt"))
            except OSError:
                pass
        return {}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".tmp")
            temp.write_text(json.dumps(self._data, ensure_ascii=False), encoding="utf-8")
            temp.replace(self.path)
        except OSError:
            logger.warning("Could not save the inbox state", exc_info=True)

    def _list(self, name: str) -> list[Any]:
        if not isinstance(self._data.get(name), list):
            self._data[name] = []
        return self._data[name]

    def _batches(self) -> dict[str, Any]:
        if not isinstance(self._data.get("batches"), dict):
            self._data["batches"] = {}
        return self._data["batches"]

    def add_listener(self, listener: Callable[[Notice], bool]) -> None:
        """``listener(notice) -> True`` when it was delivered; False keeps it queued and it is tried again later."""
        self._listeners.append(listener)

    def _now(self) -> datetime:
        return datetime.fromtimestamp(self._clock())

    # ------------------------------------------------------------------ the AI
    def _prompt(self, source: str) -> str:
        about = ""
        try:
            about = (self._about() if self._about else "") or ""
        except Exception:
            logger.debug("Inbox: no profile context", exc_info=True)
        return reader.build_prompt(source, self._now(), about[:600])

    def _read_text(self, subject: str, sender: str, body: str) -> fd.Reading:
        if self._text_ai is None:
            return fd.Reading()
        try:
            raw = self._text_ai(reader.user_text(subject, sender, body), self._prompt("email"))
        except Exception:
            logger.exception("Inbox: the AI couldn't read an email")
            return fd.Reading()
        return fd.parse(raw, self._now().date())

    # ------------------------------------------------------------------ mail
    def process_mail(self, items: list[Any], fetch: Callable[[str], dict[str, Any] | None]) -> int:
        """Read each important or urgent mail Miki hasn't seen. ``fetch(id)`` gives its full text.
        The first call only records what's already in the inbox (no flood of old mail). Returns how many it acted on."""
        acted = 0
        with self._lock:
            seen: list[str] = self._list("seen")
            first_run = not self._data.get("mail_baselined")
            fresh = [item for item in items if item.priority in {"urgent", "important"} and item.id not in seen]
            if first_run:
                seen.extend(item.id for item in fresh)
                self._data["mail_baselined"] = True
                del seen[:-MAX_SEEN]
                self._save()
                return 0
        for item in fresh:
            try:
                email = fetch(item.id) or {}
            except Exception:
                logger.debug("Inbox: couldn't fetch an email", exc_info=True)
                email = {}
            body = str(email.get("body") or item.snippet or "")
            sender = f"{item.sender_name} <{item.sender_email}>"
            reading = self._read_text(item.subject, sender, body)
            with self._lock:
                self._list("seen").append(item.id)  # once read, never again, whatever came of it
                del self._list("seen")[:-MAX_SEEN]
                self._save()
            if reading.findings or reading.summary:
                if self._act(reading, f"an email from {item.sender_name or item.sender_email}: {item.subject}"[:150], f"inbox-mail:{item.id}",
                             headline=f"📬 {item.sender_name[:40] or item.sender_email}: {item.subject[:90]}", remember=item.priority == "urgent" or bool(reading.findings)):
                    acted += 1
        return acted

    # ------------------------------------------------------------------ photos (OCR)
    def read_photo(self, image: bytes, mime: str = "image/jpeg", caption: str = "") -> InboxReply:
        """A picture you sent: dates and tasks go to your calendar and reminders, the rest is remembered."""
        if self._image_ai is None:
            return InboxReply("I can't read pictures right now.")
        try:
            raw = self._image_ai(image, mime, self._prompt("photo"), caption)
        except Exception:
            logger.exception("Inbox: the AI couldn't read a photo")
            return InboxReply("I couldn't read that picture. Try again in a moment.")
        reading = fd.parse(raw, self._now().date())
        if not reading.findings and not reading.summary:
            return InboxReply("I couldn't find any dates, deadlines or tasks in that picture. If it should be remembered anyway, tell me what it is.")
        notice = self._act(reading, "a photo you sent", f"photo:{uuid.uuid4().hex[:8]}", headline="📷 I read your picture", remember=True,
                           deliver=False)
        return InboxReply(notice.text, notice.batch, notice.link) if notice else InboxReply("I read it but couldn't do anything with it.")

    # ------------------------------------------------------------------ acting on a reading
    def _act(self, reading: fd.Reading, source: str, key: str, *, headline: str, remember: bool, deliver: bool = True) -> Notice | None:
        batch = uuid.uuid4().hex[:10]
        added, unavailable = self._add_events(reading.findings, source, batch)
        reminders = self._queue_reminders(reading.findings, batch)
        lines = [headline]
        if reading.summary:
            lines.append(reading.summary)
        if added:
            lines += ["", "📅 Added to your calendar:"] + [f"• {title} ({when})" for title, when in added]
        elif unavailable and any(f.day for f in reading.findings):
            lines += ["", "📅 I couldn't reach your calendar, so I didn't add the dates:"] + \
                     [f"• {f.title} ({_when(f)})" for f in reading.findings if f.day][:MAX_EVENTS_PER_SOURCE]
        todos = [f for f in reading.findings if f.action]
        if todos:
            lines += ["", "✅ What to do:"] + [f"• {f.title}: {f.action}" + (f" ({_when(f)})" if f.day else "") for f in todos]
        if reminders:
            lines += ["", f"⏰ I'll remind you ({', '.join(reminders)})."]
        remembered = remember and self._remember_facts(reading, source)
        if remembered:
            lines.append("🧠 I'll remember this.")
        link = next((f.link for f in todos if f.link), "")
        notice = Notice("\n".join(lines), key, batch if added else "", link)
        if not deliver:  # a photo you sent gets an answer whatever was in it
            return notice
        if added or todos or reminders:  # mail you didn't ask about only interrupts you when there's something to do
            self._deliver(notice)
            return notice
        return None

    def _tag(self, source: str) -> str:
        return f"{MARKER} {source}. Undo it from Miki."

    def _add_events(self, findings: list[fd.Finding], source: str, batch: str) -> tuple[list[tuple[str, str]], bool]:
        """Put dated findings in the calendar (skipping what's already there). Returns (what was added, calendar unreachable)."""
        dated = [f for f in findings if f.kind in {"event", "deadline"} and f.day is not None][:MAX_EVENTS_PER_SOURCE]
        if not dated:
            return [], False
        tool = self._calendar_tool()
        if tool is None:
            return [], True
        added: list[tuple[str, str]] = []
        ids: list[str] = []
        for finding in dated:
            title = f"Deadline: {finding.title}" if finding.kind == "deadline" else finding.title
            start = finding.start if finding.start is not None else REMINDER_HOUR * 60
            length = DEADLINE_MINUTES if finding.kind == "deadline" else DEFAULT_EVENT_MINUTES
            end = finding.end if finding.end is not None else min(start + length, 24 * 60 - 1)
            if self._already_there(tool, finding.day, title):
                continue
            arguments = {"title": title[:100], "start": _iso(finding.day, start), "end": _iso(finding.day, end),
                         "description": self._tag(source) + (f"\n{finding.action}" if finding.action else "") + (f"\n{finding.link}" if finding.link else "")
                         + ("\n(No time was given, so 09:00 is a placeholder.)" if finding.start is None else ""),
                         "confirm": True}  # the owner opted into automatic adds; each batch can be undone
            if finding.place:
                arguments["location"] = finding.place
            try:
                result = tool.execute("create_event", arguments)
                event_id = ((result.data or {}).get("event") or {}).get("id") if result.success else None
            except Exception:
                logger.warning("Inbox: adding a calendar event failed", exc_info=True)
                event_id = None
            if event_id:
                ids.append(str(event_id))
                added.append((title, _when(finding) if finding.start is not None else _when(finding) + ", time not given"))
        if ids:
            with self._lock:
                batches = self._batches()
                batches[batch] = {"source": source, "created": round(self._clock(), 3), "events": ids, "titles": [t for t, _ in added]}
                for old in sorted(batches, key=lambda b: batches[b].get("created", 0))[:-MAX_BATCHES]:
                    del batches[old]
                self._save()
        return added, False

    def _already_there(self, tool: Any, day: date, title: str) -> bool:
        """Is something with this title already in the calendar that day (e.g. you added it yourself, or Miki read it twice)?"""
        midnight = datetime.combine(day, dtime.min).astimezone()
        try:
            result = tool.execute("get_events", {"start": midnight.isoformat(), "end": (midnight + timedelta(days=1)).isoformat()})
        except Exception:
            return False
        if not result.success:
            return False
        wanted = title.lower().removeprefix("deadline: ").strip()
        for event in (result.data or {}).get("events", []):
            have = str(event.get("title") or "").lower().removeprefix("deadline: ").strip()
            if have and (wanted in have or have in wanted):
                return True
        return False

    def _calendar_tool(self) -> Any:
        try:
            tool = self._calendar() if self._calendar is not None else None
            return tool if tool is not None and tool.is_available() else None
        except Exception:
            logger.debug("Inbox: calendar unavailable", exc_info=True)
            return None

    def _remember_facts(self, reading: fd.Reading, source: str) -> bool:
        """Keep what was read in Miki's memory: the summary and the dated facts, written from checked fields."""
        if self._remember is None:
            return False
        facts = [reading.summary] if reading.summary else []
        facts += [f"{f.title} ({_when(f)})" if f.day else f.title for f in reading.findings]
        text = f"From {source}: " + "; ".join(facts)
        threading.Thread(target=self._remember_quietly, args=(text[:700],), name="miki-inbox-remember", daemon=True).start()
        return True

    def _remember_quietly(self, text: str) -> None:
        try:
            self._remember(text)  # type: ignore[misc]
        except Exception:
            logger.warning("Inbox: couldn't remember what it read", exc_info=True)

    # ------------------------------------------------------------------ reminders
    def _queue_reminders(self, findings: list[fd.Finding], batch: str) -> list[str]:
        """"Don't forget" texts for deadlines and things with an action: 09:00 the day before (or the day itself)."""
        now = self._now()
        queued: list[str] = []
        with self._lock:
            reminders = self._list("reminders")
            for finding in findings:
                if finding.day is None or not (finding.kind == "deadline" or finding.action):
                    continue
                at = next((moment for moment in (datetime.combine(finding.day - timedelta(days=1), dtime(REMINDER_HOUR)),
                                                 datetime.combine(finding.day, dtime(REMINDER_HOUR - 1))) if moment > now), None)
                if at is None:
                    continue
                what = f"⏰ {'Tomorrow' if at.date() < finding.day else 'Today'}: {finding.title}"
                if finding.action:
                    what += f"\n{finding.action}"
                if finding.link:
                    what += f"\n{finding.link}"
                reminders.append({"at": at.isoformat(), "text": what, "batch": batch, "link": finding.link})
                queued.append(f"{at.strftime('%a')} {at.day} {at.strftime('%b %H:%M')}")
            if queued:
                self._save()
        return queued

    def tick(self) -> int:
        """Send what's due: reminders whose time has come, and notices that were held back (quiet hours, focus)."""
        now = self._now()
        due: list[Notice] = []
        with self._lock:
            reminders = self._list("reminders")
            keep = []
            for reminder in reminders:
                try:
                    at = datetime.fromisoformat(reminder["at"])
                except (KeyError, ValueError, TypeError):
                    continue
                if at > now:
                    keep.append(reminder)
                elif now - at <= timedelta(hours=STALE_REMINDER_HOURS):
                    due.append(Notice(str(reminder.get("text", "")), f"inbox-reminder:{reminder['at']}:{reminder.get('batch', '')}",
                                      link=str(reminder.get("link") or "")))
            if len(keep) != len(reminders):
                self._data["reminders"] = keep
                self._save()
            outbox = list(self._list("outbox"))
            self._data["outbox"] = []
        sent = 0
        for notice in due + [Notice(**item) for item in outbox if isinstance(item, dict) and set(item) == {"text", "key", "batch", "link"}]:
            sent += self._deliver(notice)
        return sent

    def _deliver(self, notice: Notice) -> bool:
        """Offer it to every listener; if none delivered it, keep it for the next tick (so quiet hours delay it, never lose it)."""
        delivered = False
        for listener in list(self._listeners):
            try:
                delivered = bool(listener(notice)) or delivered
            except Exception:
                logger.exception("Inbox listener failed")
        if not delivered:
            with self._lock:
                outbox = self._list("outbox")
                if len(outbox) < MAX_OUTBOX:
                    outbox.append({"text": notice.text, "key": notice.key, "batch": notice.batch, "link": notice.link})
                    self._save()
        return delivered

    # ------------------------------------------------------------------ undo
    def undo(self, batch: str) -> str:
        """Take back everything Miki added in one batch (its calendar events and its reminders)."""
        with self._lock:
            info = self._batches().pop(batch, None)
            self._data["reminders"] = [r for r in self._list("reminders") if r.get("batch") != batch]
            self._save()
        if not info:
            return "That's already undone."
        tool = self._calendar_tool()
        removed = 0
        for event_id in info.get("events", []) if tool is not None else []:
            try:
                if tool.execute("delete_event", {"event_id": str(event_id), "confirm": True}).success:
                    removed += 1
            except Exception:
                logger.warning("Inbox: removing an event failed", exc_info=True)
        return f"↩️ Undone: removed {removed} event{'s' if removed != 1 else ''} and its reminders."


def build_inbox_service(core: Any, openai_client: Any = None, **kwargs: Any) -> InboxService | None:
    """The reader of mail and photos, or None when switched off (``MIKI_INBOX=0``)."""
    import os

    if os.getenv("MIKI_INBOX", "1").strip().lower() in {"0", "false", "no", "off"}:
        return None
    brain = getattr(core, "brain", None)
    manager = getattr(core, "memory_manager", None)
    client = openai_client or getattr(brain, "client", None)
    return InboxService(
        text_ai=reader.text_reader(brain) if brain is not None and hasattr(brain, "generate_response") else None,
        image_ai=reader.image_reader(client) if client is not None else None,
        calendar=(lambda: core.get_tool("calendar")) if hasattr(core, "get_tool") else None,
        remember=core.remember if hasattr(core, "remember") and manager is not None else None,
        about=manager.mail_context if manager is not None and hasattr(manager, "mail_context") else None,
        **kwargs,
    )
