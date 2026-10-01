"""Reading the calendar like a secretary: what's today, what's coming, when to leave. Pure functions, no network.

Calendar events arrive as dicts (``app/tools/calendar``); here they become ``Event`` with local, timezone-free times.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from app.plan.schedule import CLASS_WORDS, route_key

PLAN_MARKER = "Planned by Miki"  # events /plan wrote: they're Miki's own plan, not something to be reminded of
DEADLINE_WORDS = re.compile(
    r"deadline|\bdue\b|exam|tentamen|midterm|\bfinal\b|submit|hand.?in|registration|register|sign.?up|resit|herkansing|toets|assignment|"
    r"paper|thesis|quiz|presentation",
    re.I,
)
LEAVE_BUFFER = 10  # minutes to spare on arrival
HOME = "home"


@dataclass
class Event:
    id: str
    title: str
    start: datetime
    end: datetime
    all_day: bool = False
    location: str = ""
    description: str = ""

    @property
    def is_class(self) -> bool:
        return bool(CLASS_WORDS.search(self.title))

    @property
    def is_deadline(self) -> bool:
        return bool(DEADLINE_WORDS.search(self.title))

    @property
    def minute(self) -> int:
        return self.start.hour * 60 + self.start.minute


def _local(value: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return moment.astimezone().replace(tzinfo=None) if moment.tzinfo is not None else moment


def parse_events(raw: list[dict[str, Any]], skipped: list[dict[str, Any]] | None = None) -> list[Event]:
    """Calendar dicts -> events in time order, leaving out Miki's own plan blocks and lectures you said you'd skip."""
    skip = {(str(s.get("title", "")).lower(), int(s.get("start", -1))) for s in skipped or []}
    events: list[Event] = []
    for item in raw or []:
        if not isinstance(item, dict) or PLAN_MARKER in str(item.get("description") or ""):
            continue
        start, end = _local(item.get("start")), _local(item.get("end"))
        if start is None:
            continue
        end = end if end is not None and end >= start else start
        title = str(item.get("title") or "Event").strip()[:80]
        if (title.lower(), start.hour * 60 + start.minute) in skip or title.lower().startswith("skipped:"):
            continue
        events.append(Event(str(item.get("id") or ""), title, start, end, bool(item.get("all_day")), str(item.get("location") or "")[:80],
                            str(item.get("description") or "")))
    return sorted(events, key=lambda e: (e.start, e.title))


def on_day(events: list[Event], day: date) -> list[Event]:
    return [e for e in events if e.start.date() == day or (e.all_day and e.start.date() <= day < e.end.date())]


def deadlines(events: list[Event], today: date, within_days: int = 14) -> list[tuple[Event, int]]:
    """Things that are due or happen soon and matter (exams, deadlines, sign-ups) with the days left, soonest first."""
    found = [(e, (e.start.date() - today).days) for e in events if e.is_deadline and 0 <= (e.start.date() - today).days <= within_days]
    return sorted(found, key=lambda pair: (pair[1], pair[0].start))


def days_text(days: int, when: date) -> str:
    label = "today" if days == 0 else "tomorrow" if days == 1 else f"in {days} days"
    return f"{label} ({when.strftime('%a')} {when.day} {when.strftime('%b')})"


def route_minutes(routes: dict[tuple[str, str], int], a: str, b: str) -> int | None:
    """Known minutes between two places (None when Miki wasn't told: it never guesses a time to leave)."""
    return 0 if a == b else routes.get(route_key(a, b))


def leave_time(event: Event, routes: dict[tuple[str, str], int], study_place: str) -> tuple[datetime, int] | None:
    """For a class: when to set off from home and how long the trip is. None if it has no known trip."""
    if not event.is_class or event.all_day or not study_place or study_place == HOME:
        return None
    leg = route_minutes(routes, HOME, study_place)
    if not leg:
        return None
    return event.start - timedelta(minutes=leg + LEAVE_BUFFER), leg
