"""``data/plan.json``: the places you taught Miki, the plan waiting for your OK, and the day's plan.

    {"book":   {"routes": [["home", "school", 50], ...], "durations": {"gym": 75}, "study_place": "school"},
     "draft":  {...a plan...} or null,
     "active": {...a plan...} or null}

Writes are atomic (a temp file, then a rename), and a damaged file is kept aside instead of being overwritten.
"""

from __future__ import annotations

import json
import logging
import threading
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.plan.schedule import route_key

logger = logging.getLogger(__name__)

HOME = "home"
MAX_ROUTES = 60
MAX_DURATIONS = 40


@dataclass
class Book:
    """What Miki knows about your places: trip lengths (both ways), how long things usually take, where you study."""

    routes: dict[tuple[str, str], int] = field(default_factory=dict)
    durations: dict[str, int] = field(default_factory=dict)
    study_place: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"routes": [[a, b, m] for (a, b), m in sorted(self.routes.items())], "durations": dict(sorted(self.durations.items())),
                "study_place": self.study_place}

    @classmethod
    def from_dict(cls, data: Any) -> "Book":
        book = cls()
        if not isinstance(data, dict):
            return book
        for row in data.get("routes") or []:
            if isinstance(row, list) and len(row) == 3 and all(isinstance(x, str) for x in row[:2]) and isinstance(row[2], int):
                book.routes[route_key(row[0], row[1])] = row[2]
        for name, minutes in (data.get("durations") or {}).items():
            if isinstance(name, str) and isinstance(minutes, int):
                book.durations[name] = minutes
        book.study_place = data.get("study_place") if isinstance(data.get("study_place"), str) else None
        return book


class PlanStore:
    def __init__(self, path: str | Path = "data/plan.json") -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._data = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            pass
        logger.warning("plan.json is unreadable; keeping it as plan.corrupt and starting fresh")
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
            logger.warning("Could not save the plan", exc_info=True)

    # ------------------------------------------------------------------ places
    def book(self) -> Book:
        with self._lock:
            return Book.from_dict(self._data.get("book"))

    def save_book(self, book: Book) -> None:
        with self._lock:
            while len(book.routes) > MAX_ROUTES:
                book.routes.pop(next(iter(book.routes)))
            while len(book.durations) > MAX_DURATIONS:
                book.durations.pop(next(iter(book.durations)))
            self._data["book"] = book.to_dict()
            self._save()

    # ------------------------------------------------------------------ plans
    def get(self, slot: str) -> dict[str, Any] | None:
        """``slot`` is "draft" or "active". A copy: change it and ``put`` it back."""
        with self._lock:
            plan = self._data.get(slot)
            return deepcopy(plan) if isinstance(plan, dict) else None

    def put(self, slot: str, plan: dict[str, Any] | None) -> None:
        with self._lock:
            self._data[slot] = plan
            self._save()
