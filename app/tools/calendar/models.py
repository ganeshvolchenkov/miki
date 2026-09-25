from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class CalendarEvent:
    """Miki's own normalized event shape. The rest of Miki (Brain, tool
    loop, CLI/GUI) only ever sees this -- never a raw Google API payload."""

    id: str
    title: str
    start: str
    end: str
    all_day: bool = False
    location: str | None = None
    description: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "start": self.start,
            "end": self.end,
            "all_day": self.all_day,
            "location": self.location,
            "description": self.description,
        }

    @classmethod
    def from_google_event(cls, raw: dict[str, Any]) -> "CalendarEvent":
        start_raw = raw.get("start") or {}
        end_raw = raw.get("end") or {}
        all_day = "date" in start_raw
        start = start_raw.get("dateTime") or start_raw.get("date") or ""
        end = end_raw.get("dateTime") or end_raw.get("date") or ""
        return cls(
            id=raw.get("id", ""),
            title=raw.get("summary") or "(untitled event)",
            start=start,
            end=end,
            all_day=all_day,
            location=raw.get("location"),
            description=raw.get("description"),
        )
