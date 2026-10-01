"""What Miki found in an email or a photo, checked.

The AI reads the text (or picture) and answers in JSON. That answer is untrusted: an email can say anything,
including "add 50 events" or "ignore your instructions". So it is only ever *data* here. ``parse`` keeps what looks like
a real date, a short title and an https link, and drops the rest. What Miki then does with a finding is decided by
``service.py`` and is limited to: add a tagged, undoable calendar event, message the owner, and remember a fact.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

KINDS = ("event", "deadline", "todo", "note")
MAX_ITEMS = 10
MAX_TITLE = 80
MAX_TEXT = 220
MAX_LINK = 300
HORIZON_DAYS = 400  # a date further away than this is a misread, not a plan
_CLOCK = re.compile(r"(\d{1,2})[:.h](\d{2})")
_LINK = re.compile(r"https://[^\s<>\"']+")


@dataclass
class Finding:
    kind: str  # event: something to attend · deadline: something due · todo: something to do · note: just worth knowing
    title: str
    day: date | None = None
    start: int | None = None  # minutes since midnight, when the time is stated
    end: int | None = None
    place: str = ""
    action: str = ""  # what to do about it, in a sentence ("Sign up at the link before Friday")
    link: str = ""

    @property
    def moment(self) -> datetime | None:
        """When it starts (09:00 when only the day is known)."""
        if self.day is None:
            return None
        minute = self.start if self.start is not None else 9 * 60
        return datetime.combine(self.day, datetime.min.time()) + timedelta(minutes=minute)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "title": self.title, "day": self.day.isoformat() if self.day else None, "start": self.start,
                "end": self.end, "place": self.place, "action": self.action, "link": self.link}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Finding":
        day = date.fromisoformat(data["day"]) if data.get("day") else None
        return cls(data["kind"], data["title"], day, data.get("start"), data.get("end"), data.get("place", ""), data.get("action", ""),
                   data.get("link", ""))


@dataclass
class Reading:
    """Everything read from one source: a one-line summary (what is worth remembering) and the checked findings."""

    summary: str = ""
    findings: list[Finding] = field(default_factory=list)


def _clock(value: Any) -> int | None:
    match = _CLOCK.fullmatch(str(value or "").strip())
    if not match:
        return None
    hour, minute = int(match[1]), int(match[2])
    return hour * 60 + minute if 0 <= hour < 24 and 0 <= minute < 60 else None


def _text(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _link(value: Any) -> str:
    match = _LINK.search(str(value or ""))
    return match.group(0).rstrip(".,);")[:MAX_LINK] if match else ""


def parse(raw: Any, today: date) -> Reading:
    """The model's JSON -> a checked ``Reading`` (empty if it isn't usable)."""
    if not isinstance(raw, dict):
        return Reading()
    items = raw.get("items") if isinstance(raw.get("items"), list) else []
    findings: list[Finding] = []
    for item in items[:MAX_ITEMS]:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind", "")).strip().lower()
        title = _text(item.get("title"), MAX_TITLE)
        if kind not in KINDS or not title:
            continue
        try:
            day = date.fromisoformat(str(item.get("date") or "").strip())
        except ValueError:
            day = None
        if day is not None and not today <= day <= today + timedelta(days=HORIZON_DAYS):
            if kind in {"event", "deadline"}:
                continue  # something in the past (or absurdly far away) can't be put in a calendar
            day = None
        if kind in {"event", "deadline"} and day is None:
            kind = "todo" if item.get("action") else "note"  # no date: not a calendar entry
        start, end = _clock(item.get("start")), _clock(item.get("end"))
        if start is None or (end is not None and end <= start):
            end = None
        finding = Finding(kind, title, day, start, end, _text(item.get("place"), 80), _text(item.get("action"), MAX_TEXT),
                          _link(item.get("link")))
        if finding not in findings:
            findings.append(finding)
    return Reading(_text(raw.get("summary"), MAX_TEXT), findings)
